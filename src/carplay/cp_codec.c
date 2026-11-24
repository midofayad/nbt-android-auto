/* nbt_carplay: the compressed audio of wireless CarPlay.
 * SPDX-License-Identifier: GPL-3.0-only
 * Copyright (C) 2026 Valentin Radu
 *
 * Over Wi-Fi the iPhone sends media as AAC-LC (stream 102) and the rest (Siri, navigation, telephony) as Opus, and
 * wants the microphone as Opus (xcertplay's wireless table, AudioQualityProfiles / CarPlayAudioFormats). Each RTP
 * payload is one access unit / one Opus packet. Decoders: FAAD2 2.11.2 (GPL-2.0-or-later) for AAC-LC, with the
 * AudioSpecificConfig of the format (object type 2, the rate's index, the channels); libopus 1.5.2 (BSD-3-Clause),
 * decoding at 48 kHz (as xcertplay) and encoding the microphone (VoIP, 20 ms, 48 kbit/s up to 24 kHz).
 * Format bits (APAudioFormatIndex): LPCM 2-11 (8-44.1 kHz mono / stereo), 14 / 15 (48 kHz), AAC-LC 22 (44.1 kHz
 * stereo) / 23 (48 kHz stereo), Opus 28 / 29 / 30 (16 / 24 / 48 kHz mono); AAC-ELD (24-27, 31, 32, 43) not taken.
 */
#include <stdlib.h>
#include <string.h>
#include "cp.h"
#include "neaacdec.h"
#include "opus.h"

int audio_format(unsigned long long format, int* codec, int* rate, int* channels)
{
    static const int rates[5] = {8000, 16000, 24000, 32000, 44100};
    int bit;
    for (bit = 0; bit < 64; ++bit)
        if (format >> bit & 1)
            break;
    *codec = CODEC_NONE;
    *rate = *channels = 0;
    if (bit >= 2 && bit <= 11) {
        *codec = CODEC_LPCM;
        *channels = bit % 2 ? 2 : 1;
        *rate = rates[(bit - 2) / 2];
    } else if (bit == 14 || bit == 15) {
        *codec = CODEC_LPCM;
        *channels = bit == 15 ? 2 : 1;
        *rate = 48000;
    } else if (bit == 22 || bit == 23) {
        *codec = CODEC_AAC_LC;
        *channels = 2;
        *rate = bit == 22 ? 44100 : 48000;
    } else if (bit >= 28 && bit <= 30) {
        *codec = CODEC_OPUS;
        *channels = 1;
        *rate = bit == 28 ? 16000 : bit == 29 ? 24000 : 48000;
    }
    return *codec == CODEC_NONE ? -1 : 0;
}

const char* codec_name(int codec)
{
    return codec == CODEC_LPCM ? "LPCM" : codec == CODEC_AAC_LC ? "AAC-LC" : codec == CODEC_OPUS ? "Opus" : "?";
}

struct decoder {
    int codec, channels, rate;
    NeAACDecHandle aac;
    OpusDecoder* opus;
};

struct decoder* decoder_open(int codec, int rate, int channels)
{
    struct decoder* d = (struct decoder*)calloc(1, sizeof(*d));
    if (!d)
        return 0;
    d->codec = codec;
    if (codec == CODEC_AAC_LC) {
        static const int index_rates[] = {96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000,
                                          11025, 8000, 7350};
        unsigned char asc[2];
        unsigned long r = 0;
        unsigned char ch = 0;
        int i = 0;
        NeAACDecConfigurationPtr c;
        while (i < 13 && index_rates[i] != rate)
            ++i;
        asc[0] = (unsigned char)(2 << 3 | i >> 1);
        asc[1] = (unsigned char)((i & 1) << 7 | channels << 3);
        d->aac = NeAACDecOpen();
        if (!d->aac)
            goto fail;
        c = NeAACDecGetCurrentConfiguration(d->aac);
        c->outputFormat = FAAD_FMT_16BIT;
        c->downMatrix = 0;
        NeAACDecSetConfiguration(d->aac, c);
        if (NeAACDecInit2(d->aac, asc, 2, &r, &ch) < 0)
            goto fail;
        d->rate = (int)r;
        d->channels = ch;
    } else if (codec == CODEC_OPUS) {
        int err = 0;
        d->opus = opus_decoder_create(48000, channels, &err);
        if (!d->opus || err != OPUS_OK)
            goto fail;
        d->rate = 48000;
        d->channels = channels;
    } else
        goto fail;
    return d;
fail:
    decoder_close(d);
    return 0;
}

int decoder_rate(const struct decoder* d)
{
    return d->rate;
}

int decoder_channels(const struct decoder* d)
{
    return d->channels;
}

int decoder_run(struct decoder* d, const unsigned char* in, unsigned n, short* out, unsigned max_frames)
{
    if (d->codec == CODEC_AAC_LC) {
        NeAACDecFrameInfo info;
        void* pcm;
        unsigned frames;
        memset(&info, 0, sizeof(info));
        pcm = NeAACDecDecode(d->aac, &info, (unsigned char*)in, n);
        if (info.error || !pcm || !info.channels)
            return info.error ? -1 : 0;
        frames = (unsigned)(info.samples / info.channels);
        if (frames > max_frames)
            frames = max_frames;
        d->channels = info.channels;
        memcpy(out, pcm, frames * info.channels * sizeof(short));
        return (int)frames;
    }
    if (d->codec == CODEC_OPUS)
        return opus_decode(d->opus, in, (opus_int32)n, out, (int)max_frames, 0);
    return -1;
}

void decoder_close(struct decoder* d)
{
    if (!d)
        return;
    if (d->aac)
        NeAACDecClose(d->aac);
    if (d->opus)
        opus_decoder_destroy(d->opus);
    free(d);
}

struct encoder {
    OpusEncoder* opus;
};

struct encoder* encoder_open(int rate, int channels)
{
    struct encoder* e = (struct encoder*)calloc(1, sizeof(*e));
    int err = 0;
    if (!e)
        return 0;
    e->opus = opus_encoder_create(rate, channels, OPUS_APPLICATION_VOIP, &err);
    if (!e->opus || err != OPUS_OK) {
        encoder_close(e);
        return 0;
    }
    opus_encoder_ctl(e->opus, OPUS_SET_BITRATE(rate <= 24000 ? 48000 : 96000));
    return e;
}

int encoder_run(struct encoder* e, const short* pcm, unsigned frames, unsigned char* out, unsigned max)
{
    return opus_encode(e->opus, pcm, (int)frames, out, (opus_int32)max);
}

void encoder_close(struct encoder* e)
{
    if (!e)
        return;
    if (e->opus)
        opus_encoder_destroy(e->opus);
    free(e);
}
