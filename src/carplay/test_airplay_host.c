/* nbt_carplay host test: the AirPlay receiver (pairing, auth-setup, SETUP, screen, audio, event connection, timing)
 * on the host's loopback, driven by scripts/carplay_test.py. A fake authentication chip signs with a test RSA key.
 * SPDX-License-Identifier: GPL-3.0-only
 * Copyright (C) 2026 Valentin Radu
 *
 *   test_airplay_host STATE_DIR FIFO_OR_FILE INPUT_FIFO AUDIO_DIR [wireless]
 * wireless (scripts/carplay_wireless_test.py): the iAP2 tunnel (stream 130) is offered, and iAP2's Bluetooth link runs
 * over the first connection to 127.0.0.1:7100 (as the bridge board's relay), with test Wi-Fi parameters.
 */
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "cp.h"

struct cp_options g_opt;
volatile int g_stop;

void cp_reached(int step)
{
    say("test: step %d reached", step);
}

/* the fake chip: a fixed "certificate" and the challenge itself (padded) as the "signature" */
static const unsigned char CERT[] = "test certificate";

int acp_open(void)
{
    return 0;
}

int acp_cert(const unsigned char** cert, unsigned* n)
{
    *cert = CERT;
    *n = sizeof(CERT) - 1;
    return 0;
}

int acp_major(void)
{
    return 2;
}

int acp_sign(const unsigned char* challenge, unsigned clen, unsigned char* sig, unsigned* slen)
{
    memset(sig, 0xa5, 128);
    memcpy(sig, challenge, clen);
    *slen = 128;
    return 0;
}

int usb_gone(void)
{
    return 0;
}

static struct iap2_carplay g_cp = {"fe80::4e:42ff:fe54:1", AIRPLAY_PORT, "02:4e:42:54:00:09", 0, SOURCE_VERSION, 0, 1,
                                   {0x11, 0x22, 0x33, 0x44, 0x55, 0x66}, "NBT-test", "secret123", 36, 2};

static int relay_s = -1;

static int relay_write(void* ctx, const unsigned char* b, unsigned n)
{
    (void)ctx;
    return net_send(relay_s, b, (int)n) ? -1 : 0;
}

static int relay_read(void* ctx, unsigned char* b, unsigned n, unsigned ms)
{
    const int r = net_recv(relay_s, b, (int)n, (int)ms);
    (void)ctx;
    return r == -2 ? 0 : r <= 0 ? -1 : r;
}

static void* bluetooth(void* arg)
{
    static const struct iap2_io io = {relay_write, relay_read, 0};
    struct net_addr peer;
    const int ls = net_tcp_listen(7100, 0);
    (void)arg;
    while (ls >= 0 && !g_stop && (relay_s = net_accept(ls, &peer, 1000)) == -2) {
    }
    if (relay_s >= 0)
        say("test: Bluetooth iAP2 ended: %d", iap2_run(&io, &g_cp, IAP2_BLUETOOTH));
    return 0;
}

static void stopped(int s)
{
    (void)s;
    g_stop = 1;
}

int main(int argc, char** argv)
{
    pthread_t t;
    if (argc != 5 && !(argc == 6 && !strcmp(argv[5], "wireless")))
        return 2;
    setvbuf(stdout, 0, _IOLBF, 0);
    signal(SIGTERM, stopped);
    signal(SIGINT, stopped);
    g_opt.width = 1280;
    g_opt.height = 480;
    g_opt.fps = 60;
    g_opt.width_mm = 243;
    g_opt.height_mm = 91;
    g_opt.state_dir = argv[1];
    g_opt.dump = argv[2];
    g_opt.fifo = 0;
    g_opt.input = argv[3];
    g_opt.audio_dump = argv[4];
    g_opt.audio = 1;
    g_opt.mic = 1;
    g_opt.aux_source = 0xf6;
    mkdir(argv[1], 0700);
    if (cp_random_init() || net_init() || airplay_identity())
        return 1;
    audio_start();
    input_start();
    if (argc == 6) {
        g_opt.wireless = 1;
        snprintf(g_opt.device_id, sizeof(g_opt.device_id), "%s", g_cp.mac);
        snprintf(g_opt.bt_id, sizeof(g_opt.bt_id), "11:22:33:44:55:66");
        g_cp.public_key = airplay_public_hex();
        airplay_wireless(&g_cp);
        pthread_create(&t, 0, bluetooth, 0);
    }
    if (airplay_start(AIRPLAY_PORT))
        return 1;
    while (!g_stop)
        sleep_ms(100);
    audio_stop();
    return 0;
}
