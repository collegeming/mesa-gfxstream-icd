/* inputgrab_test.c - host-side unit tests for inputgrab_core.
 *
 * Build/run: make test
 */
#include <stdio.h>
#include <string.h>

#include "inputgrab_core.h"

static int g_failures;
static int g_checks;

#define CHECK(cond)                                                            \
    do {                                                                       \
        g_checks++;                                                            \
        if (!(cond)) {                                                         \
            g_failures++;                                                      \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);    \
        }                                                                      \
    } while (0)

#define CHECK_STR(got, want)                                                   \
    do {                                                                       \
        const char *g_ = (got);                                                \
        const char *w_ = (want);                                               \
        g_checks++;                                                            \
        if (g_ == NULL || strcmp(g_, w_) != 0) {                               \
            g_failures++;                                                      \
            fprintf(stderr, "FAIL %s:%d: got \"%s\" want \"%s\"\n", __FILE__,  \
                    __LINE__, g_ ? g_ : "(null)", w_);                         \
        }                                                                      \
    } while (0)

static void test_key_names(void)
{
    CHECK(ig_keycode_from_name("KEY_F12") == 88);
    CHECK(ig_keycode_from_name("F12") == 88);
    CHECK(ig_keycode_from_name("f12") == 88);
    CHECK(ig_keycode_from_name("KEY_VOLUMEUP") == 115);
    CHECK(ig_keycode_from_name("volumeup") == 115);
    CHECK(ig_keycode_from_name("KEY_LEFTMETA") == 125);
    CHECK(ig_keycode_from_name("KEY_A") == 30);
    CHECK(ig_keycode_from_name("BTN_LEFT") == 0x110);
    CHECK(ig_keycode_from_name("NOPE") == -1);
    CHECK(ig_keycode_from_name("") == -1);
    CHECK(ig_keycode_from_name(NULL) == -1);
    CHECK_STR(ig_key_name(88), "KEY_F12");
    CHECK_STR(ig_key_name(115), "KEY_VOLUMEUP");
    CHECK(ig_key_name(99999) == NULL);
}

static void test_key_spec(void)
{
    int codes[IG_MAX_EXIT_KEYS];
    char err[IG_MSG_MAX];
    int n;

    n = ig_parse_key_spec("KEY_F12", codes, IG_MAX_EXIT_KEYS, err, sizeof(err));
    CHECK(n == 1);
    CHECK(codes[0] == 88);

    n = ig_parse_key_spec("KEY_LEFTCTRL+KEY_LEFTALT+KEY_F12", codes,
                          IG_MAX_EXIT_KEYS, err, sizeof(err));
    CHECK(n == 3);
    CHECK(codes[0] == 29);
    CHECK(codes[1] == 56);
    CHECK(codes[2] == 88);

    n = ig_parse_key_spec("KEY_F12+BOGUS", codes, IG_MAX_EXIT_KEYS, err,
                          sizeof(err));
    CHECK(n == -1);
    CHECK(strstr(err, "unknown key name") != NULL);

    n = ig_parse_key_spec("", codes, IG_MAX_EXIT_KEYS, err, sizeof(err));
    CHECK(n == -1);
}

static void test_grab_list(void)
{
    unsigned kinds = 0;
    char err[IG_MSG_MAX];

    CHECK(ig_parse_grab_list("keyboard,touch,mouse", &kinds, err, sizeof(err)) == 0);
    CHECK(kinds == IG_KIND_ALL);

    kinds = 0;
    CHECK(ig_parse_grab_list("KBD", &kinds, err, sizeof(err)) == 0);
    CHECK(kinds == IG_KIND_KEYBOARD);

    kinds = 0;
    CHECK(ig_parse_grab_list("touchscreen,pointer", &kinds, err, sizeof(err)) == 0);
    CHECK(kinds == (IG_KIND_TOUCH | IG_KIND_MOUSE));

    kinds = 0;
    CHECK(ig_parse_grab_list("all", &kinds, err, sizeof(err)) == 0);
    CHECK(kinds == IG_KIND_ALL);

    kinds = 0;
    CHECK(ig_parse_grab_list("bogus", &kinds, err, sizeof(err)) == -1);
    CHECK(strstr(err, "unknown --grab class") != NULL);

    CHECK(ig_parse_grab_list("", &kinds, err, sizeof(err)) == -1);
}

static void test_config_defaults(void)
{
    struct ig_config cfg;
    ig_config_init(&cfg);
    CHECK(cfg.kinds == IG_KIND_ALL);
    CHECK(cfg.n_exit_keys == 1);
    CHECK(cfg.exit_keys[0] == 115);
    CHECK(cfg.exit_hold_ms == 1500);
    CHECK(cfg.scan_interval_ms == 2000);
    CHECK(cfg.touch_w == 0);
    CHECK_STR(cfg.args_file, "/data/local/tmp/inputgrab-args");
    CHECK_STR(cfg.log_file, "/data/local/tmp/inputgrab.log");
    CHECK(cfg.foreground == 0);
}

static void test_parse_args(void)
{
    struct ig_config cfg;
    char a0[] = "inputgrab";
    char a1[] = "--grab";
    char a2[] = "keyboard,touch";
    char a3[] = "--exclude";
    char a4[] = "gpio-keys,Power Button";
    char a5[] = "--exit-key";
    char a6[] = "KEY_F12";
    char a7[] = "--exit-hold-ms=300";
    char a8[] = "--args-file";
    char a9[] = "/tmp/ig-args";
    char a10[] = "--touch-size";
    char a11[] = "1600x2560";
    char a12[] = "--foreground";
    char a13[] = "--scan-interval-ms";
    char a14[] = "500";
    char *argv[] = {a0, a1,  a2,  a3,  a4,  a5,  a6,  a7,
                    a8, a9,  a10, a11, a12, a13, a14};
    int argc = (int)(sizeof(argv) / sizeof(argv[0]));

    ig_config_init(&cfg);
    CHECK(ig_parse_args(argc, argv, &cfg) == 0);
    CHECK(cfg.kinds == (IG_KIND_KEYBOARD | IG_KIND_TOUCH));
    CHECK(cfg.n_excludes == 2);
    CHECK_STR(cfg.excludes[0], "gpio-keys");
    CHECK_STR(cfg.excludes[1], "Power Button");
    CHECK(cfg.n_exit_keys == 1);
    CHECK(cfg.exit_keys[0] == 88);
    CHECK(cfg.exit_hold_ms == 300);
    CHECK_STR(cfg.args_file, "/tmp/ig-args");
    CHECK(cfg.touch_w == 1600);
    CHECK(cfg.touch_h == 2560);
    CHECK(cfg.foreground == 1);
    CHECK(cfg.scan_interval_ms == 500);

    {
        char b0[] = "inputgrab";
        char b1[] = "--nonsense";
        char *bad[] = {b0, b1};
        ig_config_init(&cfg);
        CHECK(ig_parse_args(2, bad, &cfg) == -1);
        CHECK(strstr(cfg.msg, "unknown option") != NULL);
    }
    {
        char c0[] = "inputgrab";
        char c1[] = "--grab";
        char *bad[] = {c0, c1};
        ig_config_init(&cfg);
        CHECK(ig_parse_args(2, bad, &cfg) == -1);
        CHECK(strstr(cfg.msg, "requires a value") != NULL);
    }
    {
        char d0[] = "inputgrab";
        char d1[] = "--touch-size";
        char d2[] = "notasize";
        char *bad[] = {d0, d1, d2};
        ig_config_init(&cfg);
        CHECK(ig_parse_args(3, bad, &cfg) == -1);
        CHECK(strstr(cfg.msg, "expects WxH") != NULL);
    }
    {
        char e0[] = "inputgrab";
        char e1[] = "--scan-interval-ms";
        char e2[] = "5";
        char *bad[] = {e0, e1, e2};
        ig_config_init(&cfg);
        CHECK(ig_parse_args(3, bad, &cfg) == -1);
        CHECK(strstr(cfg.msg, "out of range") != NULL);
    }
}

static void set_key(struct ig_dev_info *d, int code)
{
    ig_bits_set(d->keys.bits, IG_KEYCODES, (unsigned)code);
    d->has_ev_key = 1;
}

static void test_classification(void)
{
    struct ig_dev_info d;
    struct ig_class_result r;

    /* PC keyboard: letters + modifiers. */
    ig_dev_info_init(&d);
    snprintf(d.name, sizeof(d.name), "AT Translated Set 2 keyboard");
    set_key(&d, 30);  /* A */
    set_key(&d, 42);  /* LEFTSHIFT */
    set_key(&d, 28);  /* ENTER */
    CHECK(ig_classify(&d, &r) == IG_KIND_KEYBOARD);
    CHECK(r.kind == IG_KIND_KEYBOARD);

    /* Multi-touch screen (protocol B). */
    ig_dev_info_init(&d);
    snprintf(d.name, sizeof(d.name), "NVTCapacitiveTouchScreen");
    d.has_ev_abs = 1;
    d.has_ev_key = 1;
    ig_bits_set(d.abs.bits, IG_ABSCODES, IG_ABS_MT_POSITION_X);
    ig_bits_set(d.abs.bits, IG_ABSCODES, IG_ABS_MT_POSITION_Y);
    ig_bits_set(d.abs.bits, IG_ABSCODES, IG_ABS_MT_SLOT);
    ig_bits_set(d.abs.bits, IG_ABSCODES, IG_ABS_MT_TRACKING_ID);
    ig_bits_set(d.keys.bits, IG_KEYCODES, IG_BTN_TOUCH);
    d.has_btn_touch = 1;
    d.has_mt_pos = 1;
    d.has_mt_slot = 1;
    d.has_mt_tracking_id = 1;
    CHECK(ig_classify(&d, &r) == IG_KIND_TOUCH);
    CHECK(r.multi_touch == 1);

    /* Single-touch screen: ABS_X/ABS_Y + BTN_TOUCH. */
    ig_dev_info_init(&d);
    snprintf(d.name, sizeof(d.name), "old touchscreen");
    d.has_ev_abs = 1;
    d.has_btn_touch = 1;
    ig_bits_set(d.abs.bits, IG_ABSCODES, IG_ABS_X);
    ig_bits_set(d.abs.bits, IG_ABSCODES, IG_ABS_Y);
    set_key(&d, IG_BTN_TOUCH);
    CHECK(ig_classify(&d, &r) == IG_KIND_TOUCH);
    CHECK(r.multi_touch == 0);

    /* Relative mouse. */
    ig_dev_info_init(&d);
    snprintf(d.name, sizeof(d.name), "USB Optical Mouse");
    d.has_ev_rel = 1;
    ig_bits_set(d.rels.bits, IG_RELCODES, IG_REL_X);
    ig_bits_set(d.rels.bits, IG_RELCODES, IG_REL_Y);
    set_key(&d, IG_BTN_MOUSE);
    CHECK(ig_classify(&d, &r) == IG_KIND_MOUSE);
    CHECK(r.absolute_mouse == 0);

    /* Absolute mouse / tablet. */
    ig_dev_info_init(&d);
    snprintf(d.name, sizeof(d.name), "Wacom Pen");
    d.has_ev_abs = 1;
    d.has_btn_mouse = 1;
    ig_bits_set(d.abs.bits, IG_ABSCODES, IG_ABS_X);
    ig_bits_set(d.abs.bits, IG_ABSCODES, IG_ABS_Y);
    set_key(&d, IG_BTN_MOUSE);
    CHECK(ig_classify(&d, &r) == IG_KIND_MOUSE);
    CHECK(r.absolute_mouse == 1);

    /* gpio-keys: volume buttons only, not a typing keyboard. */
    ig_dev_info_init(&d);
    snprintf(d.name, sizeof(d.name), "gpio-keys");
    set_key(&d, 115);
    set_key(&d, 114);
    CHECK(ig_classify(&d, &r) == 0);

    /* Nothing useful. */
    ig_dev_info_init(&d);
    snprintf(d.name, sizeof(d.name), "mystery");
    CHECK(ig_classify(&d, &r) == 0);
}

static void test_selection(void)
{
    struct ig_config cfg;
    struct ig_dev_info d;
    struct ig_class_result r;

    ig_config_init(&cfg);
    ig_dev_info_init(&d);
    snprintf(d.name, sizeof(d.name), "Keyboard");
    set_key(&d, 30);
    CHECK(ig_dev_selected(&cfg, &d, &r) == 1);

    cfg.kinds = IG_KIND_TOUCH;
    CHECK(ig_dev_selected(&cfg, &d, &r) == 0);

    cfg.kinds = IG_KIND_ALL;
    snprintf(cfg.excludes[0], sizeof(cfg.excludes[0]), "keyb");
    cfg.n_excludes = 1;
    CHECK(ig_dev_selected(&cfg, &d, &r) == 0);

    snprintf(cfg.excludes[0], sizeof(cfg.excludes[0]), "KEYB");
    CHECK(ig_dev_selected(&cfg, &d, &r) == 0);

    snprintf(cfg.excludes[0], sizeof(cfg.excludes[0]), "touch");
    CHECK(ig_dev_selected(&cfg, &d, &r) == 1);

    CHECK(ig_dev_has_exit_key(&cfg, &d) == 0);
    set_key(&d, 115);
    CHECK(ig_dev_has_exit_key(&cfg, &d) == 1);
}

static void test_scaling(void)
{
    CHECK(ig_scale_abs(0, 0, 4095, 0, 4095) == 0);
    CHECK(ig_scale_abs(4095, 0, 4095, 0, 4095) == 4095);
    CHECK(ig_scale_abs(2048, 0, 4095, 0, 2559) == 1280);
    CHECK(ig_scale_abs(-10, 0, 4095, 0, 100) == 0);
    CHECK(ig_scale_abs(99999, 0, 4095, 0, 100) == 100);
    CHECK(ig_scale_abs(5, 5, 5, 0, 100) == 0);
    CHECK(ig_scale_abs(50, 0, 100, 0, 0) == 0);
    /* Non-zero source origin. */
    CHECK(ig_scale_abs(100, 100, 200, 0, 1000) == 0);
    CHECK(ig_scale_abs(150, 100, 200, 0, 1000) == 500);
}

static void test_axes(void)
{
    CHECK(ig_abs_axis(IG_ABS_X) == 1);
    CHECK(ig_abs_axis(IG_ABS_MT_POSITION_X) == 1);
    CHECK(ig_abs_axis(IG_ABS_Y) == 2);
    CHECK(ig_abs_axis(IG_ABS_MT_POSITION_Y) == 2);
    CHECK(ig_abs_axis(IG_ABS_MT_SLOT) == 0);
    CHECK(ig_abs_axis(0x28) == 0); /* ABS_PRESSURE */
}

static void test_format(void)
{
    char line[512];
    struct ig_out_desc d;

    memset(&d, 0, sizeof(d));
    d.type = IG_OUT_KEYBOARD;
    d.path = "/dev/input/event5";
    CHECK(ig_format_args_line(line, sizeof(line), &d) > 0);
    CHECK_STR(line, "--input keyboard[path=/dev/input/event5]\n");

    d.type = IG_OUT_MOUSE;
    d.path = "/dev/input/event7";
    CHECK(ig_format_args_line(line, sizeof(line), &d) > 0);
    CHECK_STR(line, "--input mouse[path=/dev/input/event7]\n");

    d.type = IG_OUT_ABS_MOUSE;
    d.path = "/dev/input/event8";
    d.width = 2560;
    d.height = 1600;
    CHECK(ig_format_args_line(line, sizeof(line), &d) > 0);
    CHECK_STR(line,
              "--input absolute-mouse[path=/dev/input/event8,width=2560,height=1600]\n");

    d.type = IG_OUT_MULTI_TOUCH;
    d.path = "/dev/input/event9";
    CHECK(ig_format_args_line(line, sizeof(line), &d) > 0);
    CHECK_STR(line,
              "--input multi-touch[path=/dev/input/event9,width=2560,height=1600]\n");

    d.type = IG_OUT_SINGLE_TOUCH;
    d.path = "/dev/input/event10";
    CHECK(ig_format_args_line(line, sizeof(line), &d) > 0);
    CHECK_STR(line,
              "--input single-touch[path=/dev/input/event10,width=2560,height=1600]\n");

    /* Truncation must fail, not silently cut a line. */
    CHECK(ig_format_args_line(line, 12, &d) == -1);
}

static void test_args_file(void)
{
    char buf[1024];
    struct ig_out_desc outs[3];
    int n;

    memset(outs, 0, sizeof(outs));
    outs[0].type = IG_OUT_KEYBOARD;
    outs[0].path = "/dev/input/event5";
    outs[1].type = IG_OUT_MULTI_TOUCH;
    outs[1].path = "/dev/input/event6";
    outs[1].width = 2560;
    outs[1].height = 1600;
    outs[2].type = IG_OUT_MOUSE;
    outs[2].path = ""; /* skipped */

    n = ig_build_args_file(buf, sizeof(buf), outs, 3);
    CHECK(n > 0);
    CHECK_STR(buf,
              "--input keyboard[path=/dev/input/event5]\n"
              "--input multi-touch[path=/dev/input/event6,width=2560,height=1600]\n");

    CHECK(ig_build_args_file(buf, 20, outs, 3) == -1);

    n = ig_build_args_file(buf, sizeof(buf), NULL, 0);
    CHECK(n == 0);
    CHECK_STR(buf, "");
}

int main(void)
{
    test_key_names();
    test_key_spec();
    test_grab_list();
    test_config_defaults();
    test_parse_args();
    test_classification();
    test_selection();
    test_scaling();
    test_axes();
    test_format();
    test_args_file();

    if (g_failures != 0) {
        fprintf(stderr, "%d/%d checks failed\n", g_failures, g_checks);
        return 1;
    }
    printf("ok: %d checks passed\n", g_checks);
    return 0;
}
