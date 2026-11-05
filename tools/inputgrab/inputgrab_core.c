/* inputgrab_core.c - pure, host-testable logic for inputgrab. */
#include "inputgrab_core.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct key_name {
    const char *name;
    int code;
};

/* Names carry their KEY_/BTN_ prefix; lookups also accept the bare name. */
static const struct key_name ig_key_names[] = {
    {"KEY_ESC", 1},
    {"KEY_1", 2},
    {"KEY_2", 3},
    {"KEY_3", 4},
    {"KEY_4", 5},
    {"KEY_5", 6},
    {"KEY_6", 7},
    {"KEY_7", 8},
    {"KEY_8", 9},
    {"KEY_9", 10},
    {"KEY_0", 11},
    {"KEY_MINUS", 12},
    {"KEY_EQUAL", 13},
    {"KEY_BACKSPACE", 14},
    {"KEY_TAB", 15},
    {"KEY_Q", 16},
    {"KEY_W", 17},
    {"KEY_E", 18},
    {"KEY_R", 19},
    {"KEY_T", 20},
    {"KEY_Y", 21},
    {"KEY_U", 22},
    {"KEY_I", 23},
    {"KEY_O", 24},
    {"KEY_P", 25},
    {"KEY_LEFTBRACE", 26},
    {"KEY_RIGHTBRACE", 27},
    {"KEY_ENTER", 28},
    {"KEY_LEFTCTRL", 29},
    {"KEY_A", 30},
    {"KEY_S", 31},
    {"KEY_D", 32},
    {"KEY_F", 33},
    {"KEY_G", 34},
    {"KEY_H", 35},
    {"KEY_J", 36},
    {"KEY_K", 37},
    {"KEY_L", 38},
    {"KEY_SEMICOLON", 39},
    {"KEY_APOSTROPHE", 40},
    {"KEY_GRAVE", 41},
    {"KEY_LEFTSHIFT", 42},
    {"KEY_BACKSLASH", 43},
    {"KEY_Z", 44},
    {"KEY_X", 45},
    {"KEY_C", 46},
    {"KEY_V", 47},
    {"KEY_B", 48},
    {"KEY_N", 49},
    {"KEY_M", 50},
    {"KEY_COMMA", 51},
    {"KEY_DOT", 52},
    {"KEY_SLASH", 53},
    {"KEY_RIGHTSHIFT", 54},
    {"KEY_KPASTERISK", 55},
    {"KEY_LEFTALT", 56},
    {"KEY_SPACE", 57},
    {"KEY_CAPSLOCK", 58},
    {"KEY_F1", 59},
    {"KEY_F2", 60},
    {"KEY_F3", 61},
    {"KEY_F4", 62},
    {"KEY_F5", 63},
    {"KEY_F6", 64},
    {"KEY_F7", 65},
    {"KEY_F8", 66},
    {"KEY_F9", 67},
    {"KEY_F10", 68},
    {"KEY_NUMLOCK", 69},
    {"KEY_SCROLLLOCK", 70},
    {"KEY_KP7", 71},
    {"KEY_KP8", 72},
    {"KEY_KP9", 73},
    {"KEY_KPMINUS", 74},
    {"KEY_KP4", 75},
    {"KEY_KP5", 76},
    {"KEY_KP6", 77},
    {"KEY_KPPLUS", 78},
    {"KEY_KP1", 79},
    {"KEY_KP2", 80},
    {"KEY_KP3", 81},
    {"KEY_KP0", 82},
    {"KEY_KPDOT", 83},
    {"KEY_F11", 87},
    {"KEY_F12", 88},
    {"KEY_KPENTER", 96},
    {"KEY_RIGHTCTRL", 97},
    {"KEY_KPSLASH", 98},
    {"KEY_SYSRQ", 99},
    {"KEY_RIGHTALT", 100},
    {"KEY_HOME", 102},
    {"KEY_UP", 103},
    {"KEY_PAGEUP", 104},
    {"KEY_LEFT", 105},
    {"KEY_RIGHT", 106},
    {"KEY_END", 107},
    {"KEY_DOWN", 108},
    {"KEY_PAGEDOWN", 109},
    {"KEY_INSERT", 110},
    {"KEY_DELETE", 111},
    {"KEY_MUTE", 113},
    {"KEY_VOLUMEDOWN", 114},
    {"KEY_VOLUMEUP", 115},
    {"KEY_POWER", 116},
    {"KEY_PAUSE", 119},
    {"KEY_LEFTMETA", 125},
    {"KEY_RIGHTMETA", 126},
    {"KEY_COMPOSE", 127},
    {"KEY_MENU", 139},
    {"KEY_COFFEE", 152},
    {"KEY_NEXTSONG", 163},
    {"KEY_PLAYPAUSE", 164},
    {"KEY_PREVIOUSSONG", 165},
    {"KEY_STOPCD", 166},
    {"KEY_HOMEPAGE", 172},
    {"KEY_PRINT", 210},
    {"BTN_LEFT", 0x110},
    {"BTN_RIGHT", 0x111},
    {"BTN_MIDDLE", 0x112},
    {"BTN_TOOL_PEN", 0x140},
    {"BTN_TOOL_FINGER", 0x145},
    {"BTN_TOUCH", 0x14a},
    {"BTN_STYLUS", 0x14b},
    {"BTN_STYLUS2", 0x14c},
};

#define IG_KEY_NAME_COUNT (int)(sizeof(ig_key_names) / sizeof(ig_key_names[0]))

/* Letters that make a device a "typing" keyboard. */
static const int ig_typing_keys[] = {
    16, 17, 18, 19, 20, 21, 22, 23, 24, 25, /* Q W E R T Y U I O P */
    30, 31, 32, 33, 34, 35, 36, 37, 38,     /* A S D F G H J K L   */
    44, 45, 46, 47, 48, 49, 50,             /* Z X C V B N M       */
    28, 57,                                 /* ENTER, SPACE        */
};

void ig_bits_set(unsigned char *bits, unsigned nbits, unsigned code)
{
    if (code >= nbits)
        return;
    bits[code / 8] |= (unsigned char)(1u << (code % 8));
}

int ig_bits_test(const unsigned char *bits, unsigned nbits, unsigned code)
{
    if (code >= nbits)
        return 0;
    return (bits[code / 8] >> (code % 8)) & 1u;
}

void ig_dev_info_init(struct ig_dev_info *d)
{
    memset(d, 0, sizeof(*d));
}

static void ig_err(char *err, size_t cap, const char *fmt, const char *arg)
{
    if (err == NULL || cap == 0)
        return;
    snprintf(err, cap, fmt, arg);
}

static int name_equal_ci(const char *a, const char *b)
{
    while (*a && *b) {
        if (toupper((unsigned char)*a) != toupper((unsigned char)*b))
            return 0;
        a++;
        b++;
    }
    return *a == '\0' && *b == '\0';
}

int ig_keycode_from_name(const char *name)
{
    char norm[64];
    size_t i;
    int k;

    if (name == NULL || *name == '\0')
        return -1;
    if (strlen(name) >= sizeof(norm))
        return -1;
    for (i = 0; name[i] != '\0'; i++)
        norm[i] = (char)toupper((unsigned char)name[i]);
    norm[i] = '\0';

    for (k = 0; k < IG_KEY_NAME_COUNT; k++) {
        if (name_equal_ci(norm, ig_key_names[k].name))
            return ig_key_names[k].code;
    }
    /* Accept the name without its KEY_/BTN_ prefix. */
    if (strncmp(norm, "KEY_", 4) != 0) {
        char with[80];
        snprintf(with, sizeof(with), "KEY_%s", norm);
        for (k = 0; k < IG_KEY_NAME_COUNT; k++) {
            if (name_equal_ci(with, ig_key_names[k].name))
                return ig_key_names[k].code;
        }
    }
    if (strncmp(norm, "BTN_", 4) != 0) {
        char with[80];
        snprintf(with, sizeof(with), "BTN_%s", norm);
        for (k = 0; k < IG_KEY_NAME_COUNT; k++) {
            if (name_equal_ci(with, ig_key_names[k].name))
                return ig_key_names[k].code;
        }
    }
    return -1;
}

const char *ig_key_name(int code)
{
    int k;
    for (k = 0; k < IG_KEY_NAME_COUNT; k++) {
        if (ig_key_names[k].code == code)
            return ig_key_names[k].name;
    }
    return NULL;
}

int ig_parse_key_spec(const char *spec, int *codes, int max_codes,
                      char *err, size_t errcap)
{
    char buf[160];
    char *p;
    int n = 0;

    if (spec == NULL || *spec == '\0') {
        ig_err(err, errcap, "empty key spec%s", "");
        return -1;
    }
    if (strlen(spec) >= sizeof(buf)) {
        ig_err(err, errcap, "key spec too long: %.40s", spec);
        return -1;
    }
    snprintf(buf, sizeof(buf), "%s", spec);

    p = buf;
    while (p != NULL && *p != '\0') {
        char *plus = strchr(p, '+');
        int code;

        if (plus != NULL)
            *plus = '\0';
        while (*p == ' ' || *p == '\t')
            p++;
        if (*p == '\0') {
            ig_err(err, errcap, "empty key in spec: %.40s", spec);
            return -1;
        }
        code = ig_keycode_from_name(p);
        if (code < 0) {
            ig_err(err, errcap, "unknown key name: %.40s", p);
            return -1;
        }
        if (n >= max_codes) {
            snprintf(err, errcap, "too many keys in combo (max %d)", max_codes);
            return -1;
        }
        if (n > 0 && codes[n - 1] == code) {
            ig_err(err, errcap, "duplicate key in combo: %.40s", p);
            return -1;
        }
        codes[n++] = code;
        p = (plus != NULL) ? plus + 1 : NULL;
    }
    return n;
}

int ig_parse_grab_list(const char *list, unsigned *kinds, char *err, size_t errcap)
{
    char buf[128];
    char *p;
    unsigned out = 0;

    if (list == NULL || *list == '\0') {
        ig_err(err, errcap, "--grab needs a non-empty list%s", "");
        return -1;
    }
    if (strlen(list) >= sizeof(buf)) {
        ig_err(err, errcap, "--grab list too long%s", "");
        return -1;
    }
    snprintf(buf, sizeof(buf), "%s", list);

    p = buf;
    while (p != NULL && *p != '\0') {
        char *comma = strchr(p, ',');
        if (comma != NULL)
            *comma = '\0';
        while (*p == ' ' || *p == '\t')
            p++;
        if (name_equal_ci(p, "keyboard") || name_equal_ci(p, "key") ||
            name_equal_ci(p, "kbd")) {
            out |= IG_KIND_KEYBOARD;
        } else if (name_equal_ci(p, "touch") || name_equal_ci(p, "touchscreen") ||
                   name_equal_ci(p, "mt")) {
            out |= IG_KIND_TOUCH;
        } else if (name_equal_ci(p, "mouse") || name_equal_ci(p, "pointer")) {
            out |= IG_KIND_MOUSE;
        } else if (name_equal_ci(p, "all")) {
            out |= IG_KIND_ALL;
        } else if (*p == '\0') {
            ig_err(err, errcap, "empty entry in --grab list: %.40s", list);
            return -1;
        } else {
            ig_err(err, errcap, "unknown --grab class: %.40s", p);
            return -1;
        }
        p = (comma != NULL) ? comma + 1 : NULL;
    }
    if (out == 0) {
        ig_err(err, errcap, "--grab matched no device class%s", "");
        return -1;
    }
    *kinds = out;
    return 0;
}

void ig_config_init(struct ig_config *cfg)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->kinds = IG_KIND_ALL;
    cfg->n_exit_keys = 1;
    cfg->exit_keys[0] = 115; /* KEY_VOLUMEUP */
    cfg->exit_hold_ms = 1500;
    cfg->scan_interval_ms = 2000;
    snprintf(cfg->args_file, sizeof(cfg->args_file),
             "%s", "/data/local/tmp/inputgrab-args");
    snprintf(cfg->log_file, sizeof(cfg->log_file),
             "%s", "/data/local/tmp/inputgrab.log");
}

static int copy_str(char *dst, size_t cap, const char *src, const char *what,
                    struct ig_config *cfg)
{
    if (strlen(src) >= cap) {
        snprintf(cfg->msg, sizeof(cfg->msg), "%s too long: %.60s", what, src);
        return -1;
    }
    snprintf(dst, cap, "%s", src);
    return 0;
}

static int parse_int(const char *s, int min, int max, int *out,
                     const char *what, struct ig_config *cfg)
{
    char *end = NULL;
    long v;
    if (s == NULL || *s == '\0') {
        snprintf(cfg->msg, sizeof(cfg->msg), "%s needs a number", what);
        return -1;
    }
    v = strtol(s, &end, 10);
    if (end == s || *end != '\0' || v < min || v > max) {
        snprintf(cfg->msg, sizeof(cfg->msg), "%s out of range: %.40s", what, s);
        return -1;
    }
    *out = (int)v;
    return 0;
}

static int split_equals(char *arg, char **value)
{
    char *eq = strchr(arg, '=');
    if (eq == NULL) {
        *value = NULL;
        return 0;
    }
    *eq = '\0';
    *value = eq + 1;
    return 1;
}

static int append_excludes(struct ig_config *cfg, const char *list,
                           const char *optname)
{
    char buf[256];
    char *p;

    if (strlen(list) >= sizeof(buf)) {
        snprintf(cfg->msg, sizeof(cfg->msg), "%s list too long", optname);
        return -1;
    }
    snprintf(buf, sizeof(buf), "%s", list);
    p = buf;
    while (p != NULL && *p != '\0') {
        char *comma = strchr(p, ',');
        if (comma != NULL)
            *comma = '\0';
        while (*p == ' ' || *p == '\t')
            p++;
        if (*p != '\0') {
            if (cfg->n_excludes >= IG_MAX_EXCLUDES) {
                snprintf(cfg->msg, sizeof(cfg->msg),
                         "too many %s patterns (max %d)", optname,
                         IG_MAX_EXCLUDES);
                return -1;
            }
            if (copy_str(cfg->excludes[cfg->n_excludes],
                         sizeof(cfg->excludes[0]), p, "exclude pattern", cfg) != 0)
                return -1;
            cfg->n_excludes++;
        }
        p = (comma != NULL) ? comma + 1 : NULL;
    }
    return 0;
}

static int parse_touch_size(struct ig_config *cfg, const char *s)
{
    int w = 0, h = 0;
    char extra = 0;
    if (sscanf(s, "%dx%d%c", &w, &h, &extra) != 2 &&
        sscanf(s, "%dX%d%c", &w, &h, &extra) != 2) {
        snprintf(cfg->msg, sizeof(cfg->msg),
                 "--touch-size expects WxH, got: %.40s", s);
        return -1;
    }
    if (w < 1 || w > 16384 || h < 1 || h > 16384) {
        snprintf(cfg->msg, sizeof(cfg->msg),
                 "--touch-size out of range: %.40s", s);
        return -1;
    }
    cfg->touch_w = w;
    cfg->touch_h = h;
    return 0;
}

int ig_parse_args(int argc, char **argv, struct ig_config *cfg)
{
    int i;

    for (i = 1; i < argc; i++) {
        char *arg = argv[i];
        char *value = NULL;
        char *eq = strchr(arg, '=');

        if (eq != NULL) {
            split_equals(arg, &value);
        }

        if (strcmp(arg, "--help") == 0 || strcmp(arg, "-h") == 0) {
            cfg->help = 1;
            continue;
        }
        if (strcmp(arg, "--foreground") == 0) {
            cfg->foreground = 1;
            continue;
        }
        if (strcmp(arg, "--grab") != 0 && strcmp(arg, "--exclude") != 0 &&
            strcmp(arg, "--exit-key") != 0 && strcmp(arg, "--exit-hold-ms") != 0 &&
            strcmp(arg, "--args-file") != 0 && strcmp(arg, "--log") != 0 &&
            strcmp(arg, "--scan-interval-ms") != 0 &&
            strcmp(arg, "--touch-size") != 0) {
            snprintf(cfg->msg, sizeof(cfg->msg), "unknown option: %.80s", arg);
            return -1;
        }
        if (value == NULL) {
            if (i + 1 >= argc) {
                snprintf(cfg->msg, sizeof(cfg->msg),
                         "option %s requires a value", arg);
                return -1;
            }
            value = argv[++i];
        }

        if (strcmp(arg, "--grab") == 0) {
            unsigned kinds = 0;
            if (ig_parse_grab_list(value, &kinds, cfg->msg, sizeof(cfg->msg)) != 0)
                return -1;
            cfg->kinds = kinds;
        } else if (strcmp(arg, "--exclude") == 0) {
            if (append_excludes(cfg, value, "--exclude") != 0)
                return -1;
        } else if (strcmp(arg, "--exit-key") == 0) {
            int codes[IG_MAX_EXIT_KEYS];
            int n = ig_parse_key_spec(value, codes, IG_MAX_EXIT_KEYS,
                                      cfg->msg, sizeof(cfg->msg));
            if (n < 0)
                return -1;
            memcpy(cfg->exit_keys, codes, sizeof(int) * (size_t)n);
            cfg->n_exit_keys = n;
        } else if (strcmp(arg, "--exit-hold-ms") == 0) {
            if (parse_int(value, 0, 60000, &cfg->exit_hold_ms,
                          "--exit-hold-ms", cfg) != 0)
                return -1;
        } else if (strcmp(arg, "--args-file") == 0) {
            if (copy_str(cfg->args_file, sizeof(cfg->args_file), value,
                         "--args-file", cfg) != 0)
                return -1;
        } else if (strcmp(arg, "--log") == 0) {
            if (copy_str(cfg->log_file, sizeof(cfg->log_file), value,
                         "--log", cfg) != 0)
                return -1;
        } else if (strcmp(arg, "--scan-interval-ms") == 0) {
            if (parse_int(value, 100, 60000, &cfg->scan_interval_ms,
                          "--scan-interval-ms", cfg) != 0)
                return -1;
        } else if (strcmp(arg, "--touch-size") == 0) {
            if (parse_touch_size(cfg, value) != 0)
                return -1;
        }
    }
    return 0;
}

static int is_typing_keyboard(const struct ig_dev_info *d)
{
    size_t k;
    if (!d->has_ev_key)
        return 0;
    for (k = 0; k < sizeof(ig_typing_keys) / sizeof(ig_typing_keys[0]); k++) {
        if (ig_bits_test(d->keys.bits, IG_KEYCODES,
                         (unsigned)ig_typing_keys[k]))
            return 1;
    }
    return 0;
}

unsigned ig_classify(const struct ig_dev_info *d, struct ig_class_result *res)
{
    struct ig_class_result r;

    memset(&r, 0, sizeof(r));
    if (res == NULL)
        res = &r;
    memset(res, 0, sizeof(*res));

    if (!d->has_ev_key && !d->has_ev_rel && !d->has_ev_abs)
        return 0;

    /* Relative pointer first: touchpads also expose ABS_MT_* but must be
     * driven as relative mice, while touchscreens never expose REL_X/Y. */
    if (d->has_ev_rel &&
        ig_bits_test(d->rels.bits, IG_RELCODES, IG_REL_X) &&
        ig_bits_test(d->rels.bits, IG_RELCODES, IG_REL_Y)) {
        res->kind = IG_KIND_MOUSE;
        res->absolute_mouse = 0;
        return res->kind;
    }

    /* Touch: touchscreens also expose BTN_TOUCH and sometimes keys. */
    if (d->has_ev_abs && d->has_mt_pos) {
        res->kind = IG_KIND_TOUCH;
        res->multi_touch = d->has_mt_slot && d->has_mt_tracking_id;
        return res->kind;
    }
    if (d->has_ev_abs && d->has_btn_touch &&
        ig_bits_test(d->abs.bits, IG_ABSCODES, IG_ABS_X) &&
        ig_bits_test(d->abs.bits, IG_ABSCODES, IG_ABS_Y)) {
        res->kind = IG_KIND_TOUCH;
        res->multi_touch = 0;
        return res->kind;
    }

    if (d->has_ev_abs && d->has_btn_mouse &&
        ig_bits_test(d->abs.bits, IG_ABSCODES, IG_ABS_X) &&
        ig_bits_test(d->abs.bits, IG_ABSCODES, IG_ABS_Y)) {
        res->kind = IG_KIND_MOUSE;
        res->absolute_mouse = 1;
        return res->kind;
    }

    if (is_typing_keyboard(d)) {
        res->kind = IG_KIND_KEYBOARD;
        return res->kind;
    }
    return 0;
}

int ig_name_excluded(const struct ig_config *cfg, const char *name)
{
    int k;
    if (name == NULL)
        return 0;
    for (k = 0; k < cfg->n_excludes; k++) {
        const char *pat = cfg->excludes[k];
        size_t plen = strlen(pat);
        size_t nlen = strlen(name);
        size_t off;
        if (plen == 0)
            continue;
        if (plen > nlen)
            continue;
        for (off = 0; off + plen <= nlen; off++) {
            size_t j;
            int match = 1;
            for (j = 0; j < plen; j++) {
                if (toupper((unsigned char)name[off + j]) !=
                    toupper((unsigned char)pat[j])) {
                    match = 0;
                    break;
                }
            }
            if (match)
                return 1;
        }
    }
    return 0;
}

int ig_dev_selected(const struct ig_config *cfg, const struct ig_dev_info *d,
                    struct ig_class_result *res)
{
    struct ig_class_result local;
    unsigned kind = ig_classify(d, &local);
    if (res != NULL)
        *res = local;
    if (kind == 0)
        return 0;
    if (!(kind & cfg->kinds))
        return 0;
    if (ig_name_excluded(cfg, d->name))
        return 0;
    return 1;
}

int ig_dev_has_exit_key(const struct ig_config *cfg, const struct ig_dev_info *d)
{
    int k;
    for (k = 0; k < cfg->n_exit_keys; k++) {
        if (ig_bits_test(d->keys.bits, IG_KEYCODES,
                         (unsigned)cfg->exit_keys[k]))
            return 1;
    }
    return 0;
}

int ig_scale_abs(int value, int src_min, int src_max, int dst_min, int dst_max)
{
    int v;
    long long num, den;

    if (src_max <= src_min || dst_max <= dst_min)
        return dst_min;
    v = value;
    if (v < src_min)
        v = src_min;
    if (v > src_max)
        v = src_max;
    num = ((long long)v - src_min) * (long long)(dst_max - dst_min);
    den = (long long)(src_max - src_min);
    return (int)(dst_min + (num + den / 2) / den);
}

int ig_abs_axis(unsigned code)
{
    if (code == IG_ABS_X || code == IG_ABS_MT_POSITION_X)
        return 1;
    if (code == IG_ABS_Y || code == IG_ABS_MT_POSITION_Y)
        return 2;
    return 0;
}

int ig_format_args_line(char *out, size_t cap, const struct ig_out_desc *d)
{
    int n = -1;

    if (out == NULL || cap == 0 || d == NULL || d->path == NULL)
        return -1;
    switch (d->type) {
    case IG_OUT_KEYBOARD:
        n = snprintf(out, cap, "--input keyboard[path=%s]\n", d->path);
        break;
    case IG_OUT_MOUSE:
        n = snprintf(out, cap, "--input mouse[path=%s]\n", d->path);
        break;
    case IG_OUT_ABS_MOUSE:
        n = snprintf(out, cap, "--input absolute-mouse[path=%s,width=%d,height=%d]\n",
                     d->path, d->width, d->height);
        break;
    case IG_OUT_MULTI_TOUCH:
        n = snprintf(out, cap,
                     "--input multi-touch[path=%s,width=%d,height=%d]\n",
                     d->path, d->width, d->height);
        break;
    case IG_OUT_SINGLE_TOUCH:
        n = snprintf(out, cap,
                     "--input single-touch[path=%s,width=%d,height=%d]\n",
                     d->path, d->width, d->height);
        break;
    }
    if (n < 0 || (size_t)n >= cap)
        return -1;
    return n;
}

int ig_build_args_file(char *out, size_t cap, const struct ig_out_desc *outs, int n)
{
    size_t used = 0;
    int i;

    if (out == NULL || cap == 0)
        return -1;
    out[0] = '\0';
    for (i = 0; i < n; i++) {
        int written;
        if (outs[i].path == NULL || outs[i].path[0] == '\0')
            continue;
        written = ig_format_args_line(out + used, cap - used, &outs[i]);
        if (written < 0)
            return -1;
        used += (size_t)written;
        if (used >= cap)
            return -1;
    }
    return (int)used;
}
