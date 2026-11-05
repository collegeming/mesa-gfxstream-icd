/* inputgrab_core.h - host-testable pure logic for the inputgrab daemon.
 *
 * This header deliberately avoids <linux/input.h> so the parsing, device
 * classification, coordinate mapping and crosvm argument formatting can be
 * compiled and unit-tested on any host (Linux, macOS, Windows/MSYS2).
 * The kernel ABI values used here are stable and defined below.
 */
#ifndef INPUTGRAB_CORE_H
#define INPUTGRAB_CORE_H

#include <stddef.h>

#define IG_PATH_MAX 160
#define IG_NAME_MAX 128
#define IG_MSG_MAX 256
#define IG_MAX_EXCLUDES 8
#define IG_MAX_EXIT_KEYS 4

#define IG_KIND_KEYBOARD 0x1u
#define IG_KIND_TOUCH 0x2u
#define IG_KIND_MOUSE 0x4u
#define IG_KIND_ALL (IG_KIND_KEYBOARD | IG_KIND_TOUCH | IG_KIND_MOUSE)

/* Compact bitmaps: key codes stop at KEY_MAX (0x2ff), abs at ABS_MAX (0x3f),
 * rel at REL_MAX (0x0f). */
#define IG_KEYCODES 0x300
#define IG_ABSCODES 0x40
#define IG_RELCODES 0x10

/* linux/input-event-codes.h subset (numeric values are ABI). */
#define IG_EV_SYN 0x00
#define IG_EV_KEY 0x01
#define IG_EV_REL 0x02
#define IG_EV_ABS 0x03
#define IG_EV_MSC 0x04

#define IG_REL_X 0x00
#define IG_REL_Y 0x01

#define IG_ABS_X 0x00
#define IG_ABS_Y 0x01
#define IG_ABS_MT_SLOT 0x2f
#define IG_ABS_MT_POSITION_X 0x35
#define IG_ABS_MT_POSITION_Y 0x36
#define IG_ABS_MT_TRACKING_ID 0x39

#define IG_BTN_MOUSE 0x110
#define IG_BTN_TOUCH 0x14a

typedef struct {
    unsigned char bits[IG_KEYCODES / 8];
} ig_key_bits;

typedef struct {
    unsigned char bits[IG_ABSCODES / 8];
} ig_abs_bits;

typedef struct {
    unsigned char bits[IG_RELCODES / 8];
} ig_rel_bits;

/* Everything the classifier needs, filled from EVIOCG* ioctls. */
struct ig_dev_info {
    char name[IG_NAME_MAX];
    char path[IG_PATH_MAX];
    int has_ev_key;
    int has_ev_rel;
    int has_ev_abs;
    ig_key_bits keys;
    ig_rel_bits rels;
    ig_abs_bits abs;
    int has_btn_touch;
    int has_btn_mouse;
    int has_mt_pos; /* ABS_MT_POSITION_X and ABS_MT_POSITION_Y */
    int has_mt_slot;
    int has_mt_tracking_id;
    int abs_x_min, abs_x_max;
    int abs_y_min, abs_y_max;
    int mt_x_min, mt_x_max;
    int mt_y_min, mt_y_max;
};

struct ig_class_result {
    unsigned kind; /* IG_KIND_* bits, 0 when the device is not usable */
    int absolute_mouse;
    int multi_touch;
};

struct ig_config {
    unsigned kinds;
    char excludes[IG_MAX_EXCLUDES][IG_NAME_MAX];
    int n_excludes;
    int exit_keys[IG_MAX_EXIT_KEYS];
    int n_exit_keys;
    int exit_hold_ms;
    char args_file[IG_PATH_MAX];
    char log_file[IG_PATH_MAX];
    int foreground;
    int scan_interval_ms;
    int touch_w; /* 0 = reuse the source axis range */
    int touch_h;
    int help;
    char msg[IG_MSG_MAX];
};

/* crosvm --input device classes we emit. */
enum ig_out_type {
    IG_OUT_KEYBOARD = 0,
    IG_OUT_MOUSE,
    IG_OUT_ABS_MOUSE,
    IG_OUT_MULTI_TOUCH,
    IG_OUT_SINGLE_TOUCH
};

struct ig_out_desc {
    enum ig_out_type type;
    const char *path;
    int width; /* only for touch / absolute mouse */
    int height;
};

/* Bitmap helpers. */
void ig_bits_set(unsigned char *bits, unsigned nbits, unsigned code);
int ig_bits_test(const unsigned char *bits, unsigned nbits, unsigned code);
void ig_dev_info_init(struct ig_dev_info *d);

/* Key names: "KEY_F12", "f12", "LEFTCTRL" (KEY_ prefix optional). */
int ig_keycode_from_name(const char *name);
const char *ig_key_name(int code);
int ig_parse_key_spec(const char *spec, int *codes, int max_codes,
                      char *err, size_t errcap);

/* Argument parsing. Returns 0 on success, -1 with cfg->msg set on error. */
void ig_config_init(struct ig_config *cfg);
int ig_parse_args(int argc, char **argv, struct ig_config *cfg);
int ig_parse_grab_list(const char *list, unsigned *kinds, char *err, size_t errcap);

/* Device classification / selection. */
unsigned ig_classify(const struct ig_dev_info *d, struct ig_class_result *res);
int ig_name_excluded(const struct ig_config *cfg, const char *name);
int ig_dev_selected(const struct ig_config *cfg, const struct ig_dev_info *d,
                    struct ig_class_result *res);
int ig_dev_has_exit_key(const struct ig_config *cfg, const struct ig_dev_info *d);

/* Coordinate mapping: clamp to [src_min, src_max] then scale to
 * [dst_min, dst_max]. Degenerate source range yields dst_min. */
int ig_scale_abs(int value, int src_min, int src_max, int dst_min, int dst_max);
/* 0 = not a coordinate axis, 1 = X, 2 = Y */
int ig_abs_axis(unsigned code);

/* crosvm argv formatting. Each line is one `--input ...` token pair, no
 * spaces inside, suitable for a whitespace-splitting wrapper. */
int ig_format_args_line(char *out, size_t cap, const struct ig_out_desc *d);
int ig_build_args_file(char *out, size_t cap, const struct ig_out_desc *outs,
                       int n);

#endif /* INPUTGRAB_CORE_H */
