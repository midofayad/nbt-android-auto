/* nbt_carplay: CarPlay sound into the car (the NBT's audio manager, A2I).
 * SPDX-License-Identifier: GPL-3.0-only
 * Copyright (C) 2026 Valentin Radu
 *
 * Streams (SETUP): 100 main (audioType media, default, alert, telephony, speechRecognition), 101 alternate (navigation
 * prompts), 102 main buffered (media, wireless). Each is RTP over UDP: a 12-byte header, the payload sealed with
 * ChaCha20-Poly1305 (AAD = header bytes 4..11, nonce = 4 zero bytes + the packet's last 8 bytes): big-endian LPCM
 * (wired), or over Wi-Fi one AAC-LC access unit (102) / one Opus packet (100, 101), decoded with cp_codec.c. Here:
 * decrypted, decoded, converted to 48 kHz stereo and queued as "media" (100 / 102 media) or "prompt" (the rest).
 * Outputs, as Android Auto's (aa_media_out): media and, while the car's source is ours (f6), the prompts on the main
 * output (input_stereo_1, music ducked under them); while another source plays, the prompts go to the prompt output
 * (input_stereo_3) over connection b8, which nbt_knob builds while /fs/ram3/nbt-aa-prompt exists and reports with
 * /fs/ram3/nbt-aa-prompt-ready. --audio-dump DIR: each stream also as a WAV file as it came (before conversion).
 */
#include <dlfcn.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "cp.h"
#include "cp_a2i.h"
#include "cp_bplist.h"
#include "cp_crypto.h"

#define RATE 48000
#define SOURCE_FILE "/fs/ram3/nbt-aa-source"
#define PROMPT_REQUEST "/fs/ram3/nbt-aa-prompt"
#define PROMPT_READY "/fs/ram3/nbt-aa-prompt-ready"

int a2i_load(struct a2i* a)
{
    void* lib = dlopen("/opt/sys/lib/liba2i_client.so", RTLD_NOW);
    if (!lib)
        lib = dlopen("liba2i_client.so", RTLD_NOW);
    if (!lib)
        return -1;
    a->open = (int (*)(void**, const struct a2i_config*))dlsym(lib, "A2I_Client_Open");
    a->close = (int (*)(void**))dlsym(lib, "A2I_Client_Close");
    a->start = (int (*)(void*))dlsym(lib, "A2I_Client_Start");
    a->stop = (int (*)(void*))dlsym(lib, "A2I_Client_Stop");
    a->notification = (int (*)(void*))dlsym(lib, "A2I_Client_GetNotification");
    a->buffer = (int (*)(void*, void**, size_t*))dlsym(lib, "A2I_Client_GetBuffer");
    a->write = (int (*)(void*, const void*, size_t, uint32_t))dlsym(lib, "A2I_Client_Out_Write");
    a->error = (const char* (*)(int))dlsym(lib, "A2I_Client_GetErrorDescription");
    return a->open && a->close && a->notification && a->buffer && a->write && a->start && a->stop ? 0 : -1;
}

/* ---- queues: 48 kHz stereo frames ---- */

struct queue {
    const char* name;
    short* buf;
    unsigned cap, rd, count, reserve;
    int waiting;
    unsigned long long in, out, dry, dropped;
    long long last_in;
};

enum { Q_MEDIA, Q_PROMPT, Q_N };
static struct queue g_q[Q_N];
static pthread_mutex_t g_m = PTHREAD_MUTEX_INITIALIZER;

static void q_init(struct queue* q, const char* name, unsigned cap, unsigned reserve)
{
    q->name = name;
    q->buf = (short*)calloc(cap * 2, sizeof(short));
    q->cap = q->buf ? cap : 0;
    q->reserve = reserve;
    q->waiting = 1;
}

static void q_push(struct queue* q, int l, int r)
{
    if (!q->cap)
        return;
    if (q->count == q->cap) {   /* full: the oldest goes */
        q->rd = (q->rd + 1) % q->cap;
        --q->count;
        ++q->dropped;
    }
    {
        const unsigned w = (q->rd + q->count) % q->cap;
        q->buf[2 * w] = (short)l;
        q->buf[2 * w + 1] = (short)r;
    }
    ++q->count;
    ++q->in;
}

static int q_take(struct queue* q, int* l, int* r)
{
    if (q->waiting) {
        if (q->count < q->reserve)
            return 0;
        q->waiting = 0;
    }
    if (!q->count) {
        q->waiting = 1;
        ++q->dry;
        return 0;
    }
    *l = q->buf[2 * q->rd];
    *r = q->buf[2 * q->rd + 1];
    q->rd = (q->rd + 1) % q->cap;
    --q->count;
    ++q->out;
    return 1;
}

/* ---- streams ---- */

struct stream {
    int on, type, queue, rate, channels, codec;
    struct decoder* dec;
    char audio_type[32];
    unsigned long long format, connection;
    unsigned char key[32];
    int data, control;
    unsigned short data_port, control_port;
    pthread_t thread;
    volatile int closing;
    /* resampling */
    int prev_l, prev_r;
    unsigned pos;
    /* timestamps: the jumps mark a new track (and are logged) */
    unsigned last_ts, last_seq;
    int last_delta, have_last, jumps_logged, control_logged;
    /* feedback */
    int started;
    unsigned first_sample;
    long long origin_ms;
    unsigned long long packets, bad;
    FILE* wav;
    unsigned wav_bytes;
};

static struct stream g_s[3];   /* types 100, 101, 102 */
volatile int audio_playing;

/* After a pause key the queued music would play on (0.2-0.4 s, plus the car's output), and the iPhone still sends a
 * little after it paused (car 2026-11-24: a short burst after the pause). So: the queue is dropped and arriving media
 * ignored until the iPhone plays again (NowPlaying after it reported the pause, or a play key); if it does not report
 * the pause within 1.5 s (the key did not pause), media is taken again (the iPhone also sends FLUSH on a pause).
 * After next / previous: no request comes (car 2026-11-24), NowPlaying reports the new track after 0.6 s while the old
 * one still arrives: stream 102 is buffered audio (audioLatencyMs 1000), sent up to a second ahead. So the quiet ends
 * at the first jump in the RTP timestamps (the new track's audio), at most 2.5 s. Ignored packets are counted in the
 * log; timestamp jumps, marker bits and control port packets are logged too. */
enum { QUIET_PAUSE = 1, QUIET_SKIP };
static struct {
    int on, saw_paused;
    long long since;
    unsigned ignored;
} g_quiet;

static void quiet_end(const char* why)   /* with g_m held */
{
    say("audio: %s, media taken (%u packets ignored in %lld ms)", why, g_quiet.ignored, ms_now() - g_quiet.since);
    g_quiet.on = 0;
}

static void quiet_start(int kind, const char* why)
{
    unsigned n;
    pthread_mutex_lock(&g_m);
    n = g_q[Q_MEDIA].count;
    g_q[Q_MEDIA].rd = g_q[Q_MEDIA].count = 0;
    g_q[Q_MEDIA].waiting = 1;
    g_quiet.on = kind;
    g_quiet.saw_paused = !audio_playing;
    g_quiet.since = ms_now();
    g_quiet.ignored = 0;
    pthread_mutex_unlock(&g_m);
    say("audio: %s key: %u queued media frames dropped", why, n);
}

void audio_media_pause(void)
{
    quiet_start(QUIET_PAUSE, "pause");
}

void audio_media_skip(void)
{
    quiet_start(QUIET_SKIP, "next / previous");
}

void audio_media_resume(void)
{
    pthread_mutex_lock(&g_m);
    if (g_quiet.on)
        quiet_end("play key");
    pthread_mutex_unlock(&g_m);
}

void audio_track_changed(void)
{
    if (g_quiet.on == QUIET_SKIP)
        say("audio: new track reported (%lld ms after the key)", ms_now() - g_quiet.since);
}

void audio_set_playing(int playing)
{
    pthread_mutex_lock(&g_m);
    audio_playing = playing;
    if (g_quiet.on == QUIET_PAUSE && !playing)
        g_quiet.saw_paused = 1;
    else if (g_quiet.on == QUIET_PAUSE && playing && g_quiet.saw_paused)
        quiet_end("playing again");
    pthread_mutex_unlock(&g_m);
}

/* with g_m held: whether arriving media is ignored now */
static int media_quiet(void)
{
    if (g_quiet.on == QUIET_PAUSE && !g_quiet.saw_paused && ms_now() - g_quiet.since > 1500)
        quiet_end("no pause reported");
    else if (g_quiet.on == QUIET_SKIP && ms_now() - g_quiet.since > 2500)
        quiet_end("no timestamp jump within 2.5 s");
    if (g_quiet.on)
        ++g_quiet.ignored;
    return g_quiet.on;
}

static void wav_header(FILE* f, int rate, int channels, unsigned bytes)
{
    unsigned char h[44];
    memcpy(h, "RIFF", 4);
    put32le(h + 4, 36 + bytes);
    memcpy(h + 8, "WAVEfmt ", 8);
    put32le(h + 16, 16);
    put16le(h + 20, 1);
    put16le(h + 22, (unsigned)channels);
    put32le(h + 24, (unsigned)rate);
    put32le(h + 28, (unsigned)(rate * channels * 2));
    put16le(h + 32, (unsigned)channels * 2);
    put16le(h + 34, 16);
    memcpy(h + 36, "data", 4);
    put32le(h + 40, bytes);
    fseek(f, 0, SEEK_SET);
    fwrite(h, 1, 44, f);
    fseek(f, 0, SEEK_END);
}

/* one frame at the stream's rate in: 48 kHz frames out (linear interpolation) */
static void resample(struct stream* s, struct queue* q, int l, int r)
{
    while (s->pos < RATE) {
        const int ol = s->prev_l + (int)((long long)(l - s->prev_l) * s->pos / RATE);
        const int orr = s->prev_r + (int)((long long)(r - s->prev_r) * s->pos / RATE);
        q_push(q, ol, orr);
        s->pos += (unsigned)s->rate;
    }
    s->pos -= RATE;
    s->prev_l = l;
    s->prev_r = r;
}

static void* stream_thread(void* arg)
{
    struct stream* s = (struct stream*)arg;
    unsigned char p[4096], pcm[4096], nonce[12];
    static short decoded[3][5760 * 2];   /* per stream type: up to 120 ms at 48 kHz, stereo */
    short* dec = decoded[s->type - 100];
    struct net_addr from;
    long long last_log = ms_now();
    while (!s->closing && !g_stop) {
        const int n = net_recvfrom(s->data, p, sizeof(p), 500, &from);
        unsigned m, i, frames;
        if (n == -2)
            continue;
        if (n < 0)
            break;
        if (n < 12 + 16 + 8)
            continue;
        memset(nonce, 0, 4);
        memcpy(nonce + 4, p + n - 8, 8);
        m = (unsigned)n - 12 - 8;
        if (aead_open(s->key, nonce, p + 4, 8, p + 12, m, pcm)) {
            if (!s->bad++)
                say("audio %d: packet not authentic", s->type);
            continue;
        }
        m -= 16;
        ++s->packets;
        {   /* timestamp jumps, the marker bit, the control port */
            const unsigned ts = get32(p + 4), seq = get16(p + 2);
            const int d = (int)(ts - s->last_ts), marker = p[1] & 0x80;
            const unsigned lost = (seq - s->last_seq) & 0xffff;   /* 1 = the next packet */
            unsigned char c[256];
            int cn;
            if (s->have_last && lost > 1 && lost < 100 && d == s->last_delta * (int)lost) {
                if (s->jumps_logged++ < 200)
                    say("audio %d: %u packet(s) lost", s->type, lost - 1);
            } else if (s->have_last && ((s->last_delta && d != s->last_delta) || marker)) {
                if (s->jumps_logged++ < 200)
                    say("audio %d: timestamp %u -> %u (%+d, usual %+d), sequence %u -> %u%s", s->type, s->last_ts, ts, d,
                        s->last_delta, s->last_seq, seq, marker ? ", marker" : "");
                if (s->queue == Q_MEDIA && d != s->last_delta) {
                    pthread_mutex_lock(&g_m);
                    if (g_quiet.on == QUIET_SKIP)
                        quiet_end("timestamp jump (the new track)");
                    pthread_mutex_unlock(&g_m);
                }
            } else if (s->have_last && !s->last_delta)
                s->last_delta = d;
            s->last_ts = ts;
            s->last_seq = seq;
            s->have_last = 1;
            if ((cn = net_recvfrom(s->control, c, sizeof(c), 1, &from)) > 0 && s->control_logged++ < 30)
                say("audio %d: control packet %02x %02x, %d bytes", s->type, c[0], c[1], cn);
        }
        if (!s->started) {
            s->started = 1;
            s->first_sample = get32(p + 4);
            s->origin_ms = ms_now();
            say("audio %d (%s): first packet, %s %u Hz %u channel(s), %u bytes", s->type, s->audio_type,
                codec_name(s->codec), s->rate, s->channels, m);
        }
        if (s->dec) {   /* AAC-LC / Opus: s->rate and s->channels become the decoder's output */
            const int k = decoder_run(s->dec, pcm, m, dec, 5760);
            if (k <= 0) {
                if (k < 0 && !s->bad++)
                    say("audio %d: %s packet not decoded", s->type, codec_name(s->codec));
                continue;
            }
            s->rate = decoder_rate(s->dec);
            s->channels = decoder_channels(s->dec);
            frames = (unsigned)k;
            m = frames * 2u * (unsigned)s->channels;
        } else {
            for (i = 0; i + 1 < m; i += 2) {   /* big-endian samples */
                const unsigned char t = pcm[i];
                pcm[i] = pcm[i + 1];
                pcm[i + 1] = t;
            }
            frames = m / (2u * (unsigned)s->channels);
        }
        if (s->wav) {
            fwrite(s->dec ? (const void*)dec : (const void*)pcm, 1, m & ~1u, s->wav);
            s->wav_bytes += m & ~1u;
        }
        pthread_mutex_lock(&g_m);
        if (s->queue == Q_MEDIA && media_quiet())
            frames = 0;   /* sent around and after the pause */
        for (i = 0; i < frames; ++i) {
            const short* f = s->dec ? dec + i * s->channels : (const short*)(pcm + i * 2 * s->channels);
            resample(s, &g_q[s->queue], f[0], s->channels > 1 ? f[1] : f[0]);
        }
        g_q[s->queue].last_in = ms_now();
        pthread_mutex_unlock(&g_m);
        if (ms_now() - last_log > 30000) {
            last_log = ms_now();
            say("audio %d: %llu packets, %llu not authentic", s->type, s->packets, s->bad);
        }
    }
    return 0;
}

int audio_stream_open(int type, const char* audio_type, unsigned long long format, unsigned long long connection,
                      const unsigned char key[32], unsigned short* data_port, unsigned short* control_port)
{
    struct stream* s;
    if (type < 100 || type > 102)
        return -1;
    audio_stream_close(type);
    s = &g_s[type - 100];
    memset(s, 0, sizeof(*s));
    s->type = type;
    snprintf(s->audio_type, sizeof(s->audio_type), "%s", audio_type);
    s->format = format;
    s->connection = connection;
    if (audio_format(format, &s->codec, &s->rate, &s->channels)) {
        say("audio %d (%s): format 0x%llx not taken", type, audio_type, format);
        return -1;
    }
    if (s->codec != CODEC_LPCM && !(s->dec = decoder_open(s->codec, s->rate, s->channels))) {
        say("audio %d (%s): no %s decoder for %d Hz %d channel(s)", type, audio_type, codec_name(s->codec), s->rate,
            s->channels);
        return -1;
    }
    if (s->dec) {
        s->rate = decoder_rate(s->dec);
        s->channels = decoder_channels(s->dec);
    }
    s->queue = (type == 100 || type == 102) && !strcmp(audio_type, "media") ? Q_MEDIA : Q_PROMPT;
    memcpy(s->key, key, 32);
    s->data = net_udp(0, &s->data_port);
    s->control = net_udp(0, &s->control_port);
    if (s->data < 0 || s->control < 0) {
        net_close(s->data);
        net_close(s->control);
        decoder_close(s->dec);
        s->dec = 0;
        return -1;
    }
    if (g_opt.audio_dump) {
        char path[300];
        snprintf(path, sizeof(path), "%s/carplay-%d-%s-%lld.wav", g_opt.audio_dump, type, audio_type, ms_now());
        s->wav = fopen(path, "wb");
        if (s->wav)
            wav_header(s->wav, s->rate, s->channels, 0);
    }
    if (pthread_create(&s->thread, 0, stream_thread, s)) {
        net_close(s->data);
        net_close(s->control);
        decoder_close(s->dec);
        s->dec = 0;
        return -1;
    }
    s->on = 1;
    *data_port = s->data_port;
    *control_port = s->control_port;
    say("audio %d (%s): %s %u Hz, %u channel(s) -> %s, ports %u / %u", type, audio_type, codec_name(s->codec),
        s->rate, s->channels, s->queue == Q_MEDIA ? "media" : "prompts", s->data_port, s->control_port);
    return 0;
}

void audio_stream_close(int type)
{
    int i;
    for (i = 0; i < 3; ++i) {
        struct stream* s = &g_s[i];
        if (!s->on || (type >= 0 && s->type != type))
            continue;
        s->closing = 1;
        pthread_join(s->thread, 0);
        net_close(s->data);
        net_close(s->control);
        if (s->wav) {
            wav_header(s->wav, s->rate, s->channels, s->wav_bytes);
            fclose(s->wav);
        }
        decoder_close(s->dec);
        s->dec = 0;
        say("audio %d (%s): closed after %llu packets", s->type, s->audio_type, s->packets);
        pthread_mutex_lock(&g_m);
        if (g_q[s->queue].count)
            g_q[s->queue].waiting = 0;   /* the rest plays even if shorter than the reserve */
        pthread_mutex_unlock(&g_m);
        s->on = 0;
    }
}

void audio_feedback(struct bp* streams, unsigned long long ntp)
{
    int i;
    for (i = 0; i < 3; ++i) {
        const struct stream* s = &g_s[i];
        struct bp* e;
        if (!s->on)
            continue;
        e = bp_add(streams, bp_dict());
        bp_set(e, "type", bp_int(s->type));
        bp_set(e, "sampleRate", bp_int(s->rate));
        if (s->started) {
            const long long elapsed = ms_now() - s->origin_ms;
            bp_set(e, "streamConnectionID", bp_uint(s->connection));
            bp_set(e, "timestamp", bp_uint(ntp));
            bp_set(e, "timestampRawNs", bp_int(ms_now() * 1000000LL));
            bp_set(e, "sampleTime", bp_uint((s->first_sample + (unsigned long long)elapsed * (unsigned)s->rate / 1000) &
                                            0xffffffffULL));
        }
    }
}

/* ---- the outputs ---- */

struct out {
    const char* name;
    char endpoint[48];
    void* h;
    unsigned primed;
    long long last_write, package_ms;
};

static struct a2i g_a2i;
static struct out g_main = {"main", "", 0, 0, 0, 0}, g_prompt = {"prompt", "", 0, 0, 0, 0};
static pthread_t g_main_thread, g_prompt_thread;
static int g_running, g_prompt_on;
static volatile int g_audio_stop;
static int g_elsewhere, g_ready;   /* prompts to the prompt output / b8 is built; under g_m */
static double g_duck = 1.0;

static int out_open(struct out* o)
{
    struct a2i_config c;
    int r;
    memset(&c, 0, sizeof(c));
    c.magic = 0x02000000;
    c.direction = 0;
    c.one = 1;
    c.buffers = 3;
    c.buffer_bytes = 4096;
    c.channels = 2;
    c.bytes_per_sample = 2;
    c.rate = RATE;
    c.endpoint = o->endpoint;
    c.interleaved = 1;
    c.cookie = 0x05555555;
    c.timeout_ms = 2000;
    o->h = 0;
    o->primed = 0;
    o->last_write = 0;
    r = g_a2i.open(&o->h, &c);
    if (r || !o->h) {
        say("audio: %s output %s: open failed (%d %s)", o->name, o->endpoint, r, g_a2i.error ? g_a2i.error(r) : "");
        o->h = 0;
        return -1;
    }
    o->package_ms = 4096 / 4 * 1000 / RATE;
    say("audio: %s output %s open", o->name, o->endpoint);
    return 0;
}

static void out_close(struct out* o)
{
    if (o->h) {
        g_a2i.close(&o->h);
        o->h = 0;
        say("audio: %s output closed", o->name);
    }
}

static void clamp(int* v)
{
    if (*v > 32767)
        *v = 32767;
    else if (*v < -32768)
        *v = -32768;
}

static void mix_main(short* o, unsigned frames)
{
    unsigned i;
    pthread_mutex_lock(&g_m);
    for (i = 0; i < frames; ++i) {
        int ml = 0, mr = 0, pl = 0, pr = 0, l, r;
        const int m = q_take(&g_q[Q_MEDIA], &ml, &mr);
        const int p = !g_elsewhere && q_take(&g_q[Q_PROMPT], &pl, &pr);
        if (p && g_duck > 0.25)
            g_duck -= 1.0 / 2400;
        else if (!p && g_duck < 1.0)
            g_duck += 1.0 / 24000;
        l = (m ? (int)(ml * g_duck) : 0) + pl;
        r = (m ? (int)(mr * g_duck) : 0) + pr;
        clamp(&l);
        clamp(&r);
        o[2 * i] = (short)l;
        o[2 * i + 1] = (short)r;
    }
    pthread_mutex_unlock(&g_m);
}

static void mix_prompt(short* o, unsigned frames)
{
    unsigned i;
    pthread_mutex_lock(&g_m);
    for (i = 0; i < frames; ++i) {
        int l = 0, r = 0;
        if (g_elsewhere && g_ready)
            q_take(&g_q[Q_PROMPT], &l, &r);
        o[2 * i] = (short)l;
        o[2 * i + 1] = (short)r;
    }
    pthread_mutex_unlock(&g_m);
}

/* one buffer: wait until the input wants it, fill, hand it over; 0 = nothing done */
static int pump(struct out* o, void (*fill)(short*, unsigned))
{
    void* b = 0;
    size_t size = 0;
    int r;
    if (o->primed >= 2) {
        const long long due = o->last_write + o->package_ms - 2, now = ms_now();
        if (due - now > 1) {
            sleep_ms((unsigned)(due - now));
            return 0;
        }
        if (g_a2i.notification(o->h) != 0) {
            sleep_ms(1);
            return 0;
        }
    }
    r = g_a2i.buffer(o->h, &b, &size);
    if (r || !b || !size) {
        sleep_ms(2);
        return 0;
    }
    fill((short*)b, (unsigned)(size / 4));
    r = g_a2i.write(o->h, b, size, 0);
    if (r) {
        say("audio: %s output: write failed (%d)", o->name, r);
        sleep_ms(20);
        return 0;
    }
    ++o->primed;
    o->last_write = ms_now();
    return 1;
}

static void raise_priority(void)
{
    struct sched_param p;
    int policy;
    pthread_getschedparam(pthread_self(), &policy, &p);
    p.sched_priority = 21;   /* above the normal applications, as aa_media_out */
    pthread_setschedparam(pthread_self(), SCHED_RR, &p);
}

static void* main_thread(void* arg)
{
    long long next = ms_now() + 30000;
    (void)arg;
    raise_priority();
    while (!g_audio_stop) {
        pump(&g_main, mix_main);
        if (ms_now() >= next) {
            int i;
            next = ms_now() + 30000;
            pthread_mutex_lock(&g_m);
            for (i = 0; i < Q_N; ++i)
                if (g_q[i].in)
                    say("audio: %s: in %llu out %llu queued %u dry %llu dropped %llu frames", g_q[i].name,
                        g_q[i].in, g_q[i].out, g_q[i].count, g_q[i].dry, g_q[i].dropped);
            pthread_mutex_unlock(&g_m);
        }
    }
    return 0;
}

/* the car's main source from nbt_knob: -1 unknown, -2 none */
static int car_source(void)
{
    char b[16];
    const int n = file_read(SOURCE_FILE, b, sizeof(b) - 1);
    if (n <= 0)
        return -1;
    b[n] = 0;
    if (!strncmp(b, "none", 4))
        return -2;
    return (int)strtoul(b, 0, 16);
}

static void* prompt_thread(void* arg)
{
    int requested = 0, gave_up = 0;
    long long requested_at = 0, last_prompt = 0, next = 0;
    (void)arg;
    raise_priority();
    while (!g_audio_stop) {
        const long long now = ms_now();
        if (now >= next) {
            const int src = car_source();
            const int ours = src == -1 || src == (int)g_opt.aux_source;
            int pending, want;
            next = now + 50;
            pthread_mutex_lock(&g_m);
            pending = g_q[Q_PROMPT].count || now - g_q[Q_PROMPT].last_in < 300;
            if (pending)
                last_prompt = now;
            if (!pending && !requested && now - last_prompt >= 800) {
                g_elsewhere = !ours;   /* decided between prompts only */
                gave_up = 0;
            }
            want = g_elsewhere && !gave_up && (pending || now - last_prompt < 800);
            pthread_mutex_unlock(&g_m);
            if (want && !requested) {
                flag_set(PROMPT_REQUEST, 0);
                requested = 1;
                requested_at = now;
                say("audio: prompt while source %02x plays: b8 requested", src < 0 ? 0xff : src);
                if (!g_prompt.h)
                    out_open(&g_prompt);
            }
            if (requested) {
                const int ready = access(PROMPT_READY, F_OK) == 0;
                pthread_mutex_lock(&g_m);
                if (ready && !g_ready)
                    say("audio: b8 built after %lld ms", now - requested_at);
                g_ready = ready && g_prompt.h;
                if (!ready && now - requested_at > 2500 && !gave_up) {
                    gave_up = 1;
                    g_elsewhere = 0;
                    say("audio: b8 not built within 2.5 s: prompts on the main output");
                }
                pthread_mutex_unlock(&g_m);
            }
            if (requested && !want) {
                flag_clear(PROMPT_REQUEST);
                requested = 0;
                pthread_mutex_lock(&g_m);
                g_ready = 0;
                pthread_mutex_unlock(&g_m);
                out_close(&g_prompt);
                say("audio: prompt over, b8 released");
            }
        }
        if (g_prompt.h)
            pump(&g_prompt, mix_prompt);
        else
            sleep_ms(20);
    }
    if (requested)
        flag_clear(PROMPT_REQUEST);
    out_close(&g_prompt);
    return 0;
}

int audio_start(void)
{
    if (g_running)
        return 0;
    q_init(&g_q[Q_MEDIA], "media", RATE * 2, RATE / 10);
    q_init(&g_q[Q_PROMPT], "prompts", RATE * 3, RATE / 20);
    if (a2i_load(&g_a2i)) {
        say("audio: no liba2i_client.so: the streams are taken but not played");
        return -1;
    }
    if (!g_opt.media_out || !g_opt.media_out[0]) {
        say("audio: no media output");
        return -1;
    }
    snprintf(g_main.endpoint, sizeof(g_main.endpoint), "%s", g_opt.media_out);
    if (out_open(&g_main))
        return -1;
    g_audio_stop = 0;
    if (pthread_create(&g_main_thread, 0, main_thread, 0)) {
        out_close(&g_main);
        return -1;
    }
    if (g_opt.prompt_out && g_opt.prompt_out[0]) {
        snprintf(g_prompt.endpoint, sizeof(g_prompt.endpoint), "%s", g_opt.prompt_out);
        flag_clear(PROMPT_REQUEST);
        g_prompt_on = !pthread_create(&g_prompt_thread, 0, prompt_thread, 0);
    }
    g_running = 1;
    return 0;
}

void audio_stop(void)
{
    audio_stream_close(-1);
    if (!g_running)
        return;
    g_audio_stop = 1;
    pthread_join(g_main_thread, 0);
    if (g_prompt_on)
        pthread_join(g_prompt_thread, 0);
    out_close(&g_main);
    g_running = 0;
}
