/* inputgrab - root input grab daemon for Android/DroidVM.
 *
 * Opens the physical input devices, takes them away from Android with
 * EVIOCGRAB, mirrors every event into a uinput device of the same name and
 * publishes the resulting /dev/input/eventN paths as crosvm --input
 * arguments.
 *
 * Run as root: inputgrab --grab keyboard,touch,mouse \
 *                        --args-file /data/local/tmp/inputgrab-args
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include "inputgrab_core.h"

#ifndef UI_DEV_SETUP
#define UI_DEV_SETUP _IOW(UINPUT_IOCTL_BASE, 3, struct uinput_setup)
#endif
#ifndef UI_ABS_SETUP
#define UI_ABS_SETUP _IOW(UINPUT_IOCTL_BASE, 4, struct uinput_abs_setup)
#endif
#ifndef UI_DEV_CREATE
#define UI_DEV_CREATE _IO(UINPUT_IOCTL_BASE, 1)
#endif
#ifndef UI_DEV_DESTROY
#define UI_DEV_DESTROY _IO(UINPUT_IOCTL_BASE, 2)
#endif
#ifndef UI_GET_SYSNAME
#define UI_GET_SYSNAME(len) _IOC(_IOC_READ, UINPUT_IOCTL_BASE, 101, len)
#endif

#define IG_MAX_DEV 32
#define IG_VENDOR 0x1d6b /* Linux Foundation; tags every device we create */
#define IG_PRODUCT_BASE 0x1d00
#define IG_VERSION 1
#define IG_MAX_MSG 512

enum ig_log_level {
    IG_LOG_INFO = 0,
    IG_LOG_WARN,
    IG_LOG_ERROR,
    IG_LOG_DEBUG
};

struct ig_src {
    int used;
    int fd;
    char path[IG_PATH_MAX];
    char name[IG_NAME_MAX];
    unsigned kind;
    int out_idx; /* -1 for monitor-only devices */
    int monitor_only;
    struct ig_dev_info info;
};

struct ig_out {
    int used;
    int fd;
    char path[IG_PATH_MAX];
    char sysname[64];
    char name[IG_NAME_MAX];
    enum ig_out_type type;
    int width; /* advertised to crosvm / uinput for touch */
    int height;
    int src_x_min, src_x_max, src_y_min, src_y_max;
    int dst_x_max, dst_y_max;
    int needs_map;
};

static struct ig_config g_cfg;
static struct ig_src g_src[IG_MAX_DEV];
static struct ig_out g_out[IG_MAX_DEV];
static int g_logfd = -1;
static int g_sigpipe[2] = {-1, -1};
static volatile sig_atomic_t g_stop = 0;
static char g_exit_reason[128] = "signal";
static long long g_start_ms = 0;
static int g_forward_drops = 0;
static char g_failed_paths[IG_MAX_DEV][IG_PATH_MAX];
static int g_failed_errno[IG_MAX_DEV];
static int g_failed_count = 0;

static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
}

static void sleep_ms(int ms)
{
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (long)(ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}

static void log_msg(int level, const char *fmt, ...)
{
    static const char *names[] = {"INFO", "WARN", "ERROR", "DEBUG"};
    char msg[IG_MAX_MSG];
    char line[IG_MAX_MSG + 64];
    char ts[32];
    time_t now = time(NULL);
    struct tm tmv;
    va_list ap;
    int n;

    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);

    localtime_r(&now, &tmv);
    strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tmv);
    n = snprintf(line, sizeof(line), "[%s] %s: %s\n", ts,
                 names[level < 0 || level > IG_LOG_DEBUG ? IG_LOG_INFO : level],
                 msg);
    if (n > 0) {
        ssize_t wrote;
        if (g_logfd >= 0) {
            wrote = write(g_logfd, line, (size_t)n);
            (void)wrote;
        }
        if (g_cfg.foreground) {
            wrote = write(STDERR_FILENO, line, (size_t)n);
            (void)wrote;
        }
    }
}

static void on_signal(int sig)
{
    unsigned char c = (unsigned char)sig;
    if (g_sigpipe[1] >= 0) {
        ssize_t wrote = write(g_sigpipe[1], &c, 1);
        (void)wrote;
    }
}

static void log_once_failure(const char *path, int err)
{
    int k;
    for (k = 0; k < g_failed_count; k++) {
        if (strcmp(g_failed_paths[k], path) == 0 && g_failed_errno[k] == err)
            return;
    }
    if (g_failed_count >= IG_MAX_DEV) {
        /* Keep the table bounded; log the rest with a marker. */
        log_msg(IG_LOG_DEBUG, "%s: open/ioctl failed: %s", path, strerror(err));
        return;
    }
    snprintf(g_failed_paths[g_failed_count], IG_PATH_MAX, "%s", path);
    g_failed_errno[g_failed_count] = err;
    g_failed_count++;
    log_msg(IG_LOG_WARN, "%s: open/ioctl failed: %s (check root + SELinux)", path,
            strerror(err));
}

/* ---------------------------------------------------------------- uinput */

static int uinput_str_cap(const char *name)
{
    return name != NULL && name[0] != '\0';
}

static int abs_range_for(const struct ig_dev_info *info, const struct ig_config *cfg,
                         int axis_y, int *src_min, int *src_max, int *dst_max)
{
    int use_mt = info->has_mt_pos;
    int smin, smax;

    if (axis_y) {
        smin = use_mt ? info->mt_y_min : info->abs_y_min;
        smax = use_mt ? info->mt_y_max : info->abs_y_max;
    } else {
        smin = use_mt ? info->mt_x_min : info->abs_x_min;
        smax = use_mt ? info->mt_x_max : info->abs_x_max;
    }
    if (smax <= smin) {
        /* No usable range: fall back to a synthetic 0..4095. */
        smin = 0;
        smax = 4095;
    }
    *src_min = smin;
    *src_max = smax;
    if (axis_y)
        *dst_max = cfg->touch_h > 0 ? cfg->touch_h - 1 : smax - smin;
    else
        *dst_max = cfg->touch_w > 0 ? cfg->touch_w - 1 : smax - smin;
    if (*dst_max < 1)
        *dst_max = 1;
    return 0;
}

static int find_event_path(const char *sysname, char *out, size_t cap)
{
    char dir[160];
    int attempt;

    snprintf(dir, sizeof(dir), "/sys/class/input/%s", sysname);
    for (attempt = 0; attempt < 5; attempt++) {
        DIR *d = opendir(dir);
        if (d != NULL) {
            struct dirent *de;
            while ((de = readdir(d)) != NULL) {
                if (strncmp(de->d_name, "event", 5) == 0 &&
                    strlen(de->d_name) < 32) {
                    snprintf(out, cap, "/dev/input/%s", de->d_name);
                    closedir(d);
                    return 0;
                }
            }
            closedir(d);
        }
        sleep_ms(20);
    }
    return -1;
}

static void destroy_out(struct ig_out *o)
{
    if (!o->used)
        return;
    if (o->fd >= 0) {
        (void)ioctl(o->fd, UI_DEV_DESTROY);
        close(o->fd);
        o->fd = -1;
    }
    log_msg(IG_LOG_INFO, "uinput: destroyed %s (%s)", o->path[0] ? o->path : "?",
            o->name);
    o->used = 0;
}

static int create_uinput_for(int src_fd, const struct ig_dev_info *info,
                             const struct ig_class_result *cls, struct ig_out *o)
{
    int fd;
    int code;
    int have_keys = 0;
    int have_rels = 0;
    int have_abs = 0;
    struct uinput_setup setup;

    memset(o, 0, sizeof(*o));
    o->fd = -1;

    fd = open("/dev/uinput", O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) {
        log_once_failure("/dev/uinput", errno);
        return -1;
    }

    for (code = 0; code < IG_KEYCODES; code++)
        if (ig_bits_test(info->keys.bits, IG_KEYCODES, (unsigned)code))
            have_keys = 1;
    for (code = 0; code < IG_RELCODES; code++)
        if (ig_bits_test(info->rels.bits, IG_RELCODES, (unsigned)code))
            have_rels = 1;
    for (code = 0; code < IG_ABSCODES; code++)
        if (ig_bits_test(info->abs.bits, IG_ABSCODES, (unsigned)code))
            have_abs = 1;

    if (have_keys && ioctl(fd, UI_SET_EVBIT, EV_KEY) < 0)
        log_msg(IG_LOG_WARN, "/dev/uinput: UI_SET_EVBIT EV_KEY: %s", strerror(errno));
    if (have_rels && ioctl(fd, UI_SET_EVBIT, EV_REL) < 0)
        log_msg(IG_LOG_WARN, "/dev/uinput: UI_SET_EVBIT EV_REL: %s", strerror(errno));
    if (have_abs && ioctl(fd, UI_SET_EVBIT, EV_ABS) < 0)
        log_msg(IG_LOG_WARN, "/dev/uinput: UI_SET_EVBIT EV_ABS: %s", strerror(errno));

    for (code = 0; code < IG_KEYCODES; code++) {
        if (!ig_bits_test(info->keys.bits, IG_KEYCODES, (unsigned)code))
            continue;
        if (ioctl(fd, UI_SET_KEYBIT, code) < 0)
            log_msg(IG_LOG_DEBUG, "UI_SET_KEYBIT(%d): %s", code, strerror(errno));
    }
    for (code = 0; code < IG_RELCODES; code++) {
        if (!ig_bits_test(info->rels.bits, IG_RELCODES, (unsigned)code))
            continue;
        if (ioctl(fd, UI_SET_RELBIT, code) < 0)
            log_msg(IG_LOG_DEBUG, "UI_SET_RELBIT(%d): %s", code, strerror(errno));
    }

    o->needs_map = 0;
    for (code = 0; code < IG_ABSCODES; code++) {
        struct input_absinfo ai;
        struct uinput_abs_setup setup_abs;

        if (!ig_bits_test(info->abs.bits, IG_ABSCODES, (unsigned)code))
            continue;
        memset(&ai, 0, sizeof(ai));
        if (ioctl(src_fd, EVIOCGABS(code), &ai) < 0) {
            log_msg(IG_LOG_DEBUG, "EVIOCGABS(%d): %s", code, strerror(errno));
            continue;
        }
        if (cls->kind == IG_KIND_TOUCH && ig_abs_axis((unsigned)code) != 0) {
            int axis_y = ig_abs_axis((unsigned)code) == 2;
            int smin, smax, dmax;
            abs_range_for(info, &g_cfg, axis_y, &smin, &smax, &dmax);
            ai.minimum = 0;
            ai.maximum = dmax;
            ai.fuzz = 0;
            ai.flat = 0;
            ai.resolution = 0;
            o->needs_map = 1;
        } else if (cls->kind == IG_KIND_MOUSE && cls->absolute_mouse &&
                   ig_abs_axis((unsigned)code) != 0) {
            int axis_y = ig_abs_axis((unsigned)code) == 2;
            int smin, smax, dmax;
            abs_range_for(info, &g_cfg, axis_y, &smin, &smax, &dmax);
            ai.minimum = 0;
            ai.maximum = dmax;
            o->needs_map = 1;
        }
        memset(&setup_abs, 0, sizeof(setup_abs));
        setup_abs.code = (uint16_t)code;
        setup_abs.absinfo = ai;
        if (ioctl(fd, UI_ABS_SETUP, &setup_abs) < 0) {
            log_msg(IG_LOG_WARN, "/dev/uinput: UI_ABS_SETUP(%d): %s", code,
                    strerror(errno));
            (void)ioctl(fd, UI_SET_ABSBIT, code);
        }
    }

    if (cls->kind == IG_KIND_TOUCH) {
        abs_range_for(info, &g_cfg, 0, &o->src_x_min, &o->src_x_max, &o->dst_x_max);
        abs_range_for(info, &g_cfg, 1, &o->src_y_min, &o->src_y_max, &o->dst_y_max);
        o->width = o->dst_x_max + 1;
        o->height = o->dst_y_max + 1;
        if (ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT) < 0)
            log_msg(IG_LOG_DEBUG, "UI_SET_PROPBIT(DIRECT): %s", strerror(errno));
    } else if (cls->kind == IG_KIND_MOUSE) {
        if (cls->absolute_mouse) {
            abs_range_for(info, &g_cfg, 0, &o->src_x_min, &o->src_x_max,
                          &o->dst_x_max);
            abs_range_for(info, &g_cfg, 1, &o->src_y_min, &o->src_y_max,
                          &o->dst_y_max);
            o->width = o->dst_x_max + 1;
            o->height = o->dst_y_max + 1;
        }
        if (ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_POINTER) < 0)
            log_msg(IG_LOG_DEBUG, "UI_SET_PROPBIT(POINTER): %s", strerror(errno));
    }

    memset(&setup, 0, sizeof(setup));
    setup.id.bustype = BUS_VIRTUAL;
    setup.id.vendor = IG_VENDOR;
    setup.id.product = (uint16_t)(IG_PRODUCT_BASE +
                                  (cls->kind == IG_KIND_KEYBOARD ? 1
                                   : cls->kind == IG_KIND_TOUCH   ? 2
                                                                  : 3));
    setup.id.version = IG_VERSION;
    snprintf(setup.name, sizeof(setup.name), "%.79s",
             uinput_str_cap(info->name) ? info->name : "inputgrab virtual device");
    if (ioctl(fd, UI_DEV_SETUP, &setup) < 0) {
        log_msg(IG_LOG_ERROR, "/dev/uinput: UI_DEV_SETUP failed: %s", strerror(errno));
        close(fd);
        return -1;
    }
    if (ioctl(fd, UI_DEV_CREATE) < 0) {
        log_msg(IG_LOG_ERROR, "/dev/uinput: UI_DEV_CREATE failed: %s", strerror(errno));
        close(fd);
        return -1;
    }

    memset(o->sysname, 0, sizeof(o->sysname));
    if (ioctl(fd, UI_GET_SYSNAME(64), o->sysname) < 0) {
        log_msg(IG_LOG_WARN, "/dev/uinput: UI_GET_SYSNAME failed: %s", strerror(errno));
        o->sysname[0] = '\0';
    }
    o->fd = fd;
    o->used = 1;
    snprintf(o->name, sizeof(o->name), "%s", setup.name);
    switch (cls->kind) {
    case IG_KIND_KEYBOARD:
        o->type = IG_OUT_KEYBOARD;
        break;
    case IG_KIND_TOUCH:
        o->type = cls->multi_touch ? IG_OUT_MULTI_TOUCH : IG_OUT_SINGLE_TOUCH;
        break;
    default:
        o->type = cls->absolute_mouse ? IG_OUT_ABS_MOUSE : IG_OUT_MOUSE;
        break;
    }

    if (o->sysname[0] != '\0' &&
        find_event_path(o->sysname, o->path, sizeof(o->path)) == 0) {
        log_msg(IG_LOG_INFO, "uinput: %s -> %s (%s)", setup.name, o->path, o->sysname);
    } else {
        log_msg(IG_LOG_WARN, "uinput: %s created but event node not found (%s)",
                setup.name, o->sysname);
        o->path[0] = '\0';
    }
    return 0;
}

static void uinput_write_event(int fd, const struct input_event *ev)
{
    int tries;

    for (tries = 0; tries < 20; tries++) {
        ssize_t w = write(fd, ev, sizeof(*ev));
        if (w == (ssize_t)sizeof(*ev))
            return;
        if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            sleep_ms(2);
            continue;
        }
        if (w < 0 && errno == EINTR)
            continue;
        g_forward_drops++;
        if (g_forward_drops == 1 || (g_forward_drops % 200) == 0)
            log_msg(IG_LOG_WARN, "uinput write failed (%d drops): %s",
                    g_forward_drops, strerror(errno));
        return;
    }
    g_forward_drops++;
    if ((g_forward_drops % 200) == 1)
        log_msg(IG_LOG_WARN, "uinput write timed out (%d drops)", g_forward_drops);
}

/* ------------------------------------------------------------- device scan */

static int src_index_by_path(const char *path)
{
    int i;
    for (i = 0; i < IG_MAX_DEV; i++) {
        if (g_src[i].used && strcmp(g_src[i].path, path) == 0)
            return i;
    }
    return -1;
}

static int free_src_slot(void)
{
    int i;
    for (i = 0; i < IG_MAX_DEV; i++)
        if (!g_src[i].used)
            return i;
    return -1;
}

static int out_index_by_path(const char *path)
{
    int i;
    for (i = 0; i < IG_MAX_DEV; i++)
        if (g_out[i].used && strcmp(g_out[i].path, path) == 0)
            return i;
    return -1;
}

static int free_out_slot(void)
{
    int i;
    for (i = 0; i < IG_MAX_DEV; i++)
        if (!g_out[i].used)
            return i;
    return -1;
}

static int is_our_uinput(int fd)
{
    struct input_id id;
    memset(&id, 0, sizeof(id));
    if (ioctl(fd, EVIOCGID, &id) < 0)
        return 0;
    return id.vendor == IG_VENDOR && id.product >= IG_PRODUCT_BASE &&
           id.product < IG_PRODUCT_BASE + 16;
}

static int load_dev_info(int fd, const char *path, struct ig_dev_info *info)
{
    unsigned char evraw[(EV_MAX + 8) / 8];
    char name[256];
    int code;

    ig_dev_info_init(info);
    snprintf(info->path, sizeof(info->path), "%s", path);

    memset(name, 0, sizeof(name));
    if (ioctl(fd, EVIOCGNAME(sizeof(name) - 1), name) < 0)
        snprintf(name, sizeof(name), "unknown");
    snprintf(info->name, sizeof(info->name), "%s", name);

    memset(evraw, 0, sizeof(evraw));
    if (ioctl(fd, EVIOCGBIT(0, sizeof(evraw)), evraw) < 0)
        return -1;
    info->has_ev_key = ig_bits_test(evraw, EV_MAX + 1, EV_KEY);
    info->has_ev_rel = ig_bits_test(evraw, EV_MAX + 1, EV_REL);
    info->has_ev_abs = ig_bits_test(evraw, EV_MAX + 1, EV_ABS);

    if (info->has_ev_key) {
        unsigned char raw[(KEY_MAX + 8) / 8];
        memset(raw, 0, sizeof(raw));
        if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(raw)), raw) == 0) {
            for (code = 0; code < IG_KEYCODES && code <= KEY_MAX; code++) {
                if (ig_bits_test(raw, KEY_MAX + 1, (unsigned)code))
                    ig_bits_set(info->keys.bits, IG_KEYCODES, (unsigned)code);
            }
        }
    }
    if (info->has_ev_rel) {
        unsigned char raw[(REL_MAX + 8) / 8];
        memset(raw, 0, sizeof(raw));
        if (ioctl(fd, EVIOCGBIT(EV_REL, sizeof(raw)), raw) == 0) {
            for (code = 0; code < IG_RELCODES && code <= REL_MAX; code++) {
                if (ig_bits_test(raw, REL_MAX + 1, (unsigned)code))
                    ig_bits_set(info->rels.bits, IG_RELCODES, (unsigned)code);
            }
        }
    }
    if (info->has_ev_abs) {
        unsigned char raw[(ABS_MAX + 8) / 8];
        struct input_absinfo ai;
        memset(raw, 0, sizeof(raw));
        if (ioctl(fd, EVIOCGBIT(EV_ABS, sizeof(raw)), raw) == 0) {
            for (code = 0; code < IG_ABSCODES && code <= ABS_MAX; code++) {
                if (ig_bits_test(raw, ABS_MAX + 1, (unsigned)code))
                    ig_bits_set(info->abs.bits, IG_ABSCODES, (unsigned)code);
            }
        }
        memset(&ai, 0, sizeof(ai));
        if (ioctl(fd, EVIOCGABS(ABS_X), &ai) == 0) {
            info->abs_x_min = ai.minimum;
            info->abs_x_max = ai.maximum;
        }
        memset(&ai, 0, sizeof(ai));
        if (ioctl(fd, EVIOCGABS(ABS_Y), &ai) == 0) {
            info->abs_y_min = ai.minimum;
            info->abs_y_max = ai.maximum;
        }
        memset(&ai, 0, sizeof(ai));
        if (ioctl(fd, EVIOCGABS(ABS_MT_POSITION_X), &ai) == 0) {
            info->mt_x_min = ai.minimum;
            info->mt_x_max = ai.maximum;
        }
        memset(&ai, 0, sizeof(ai));
        if (ioctl(fd, EVIOCGABS(ABS_MT_POSITION_Y), &ai) == 0) {
            info->mt_y_min = ai.minimum;
            info->mt_y_max = ai.maximum;
        }
    }

    info->has_btn_touch =
        ig_bits_test(info->keys.bits, IG_KEYCODES, IG_BTN_TOUCH);
    info->has_btn_mouse =
        ig_bits_test(info->keys.bits, IG_KEYCODES, IG_BTN_MOUSE);
    info->has_mt_pos = ig_bits_test(info->abs.bits, IG_ABSCODES,
                                    IG_ABS_MT_POSITION_X) &&
                       ig_bits_test(info->abs.bits, IG_ABSCODES,
                                    IG_ABS_MT_POSITION_Y);
    info->has_mt_slot =
        ig_bits_test(info->abs.bits, IG_ABSCODES, IG_ABS_MT_SLOT);
    info->has_mt_tracking_id =
        ig_bits_test(info->abs.bits, IG_ABSCODES, IG_ABS_MT_TRACKING_ID);
    return 0;
}

static int publish_args(void);

static void remove_src(int idx, const char *why)
{
    struct ig_src *s = &g_src[idx];
    if (!s->used)
        return;
    log_msg(IG_LOG_INFO, "device: %s (%s) removed: %s", s->path, s->name, why);
    if (s->fd >= 0)
        close(s->fd);
    s->fd = -1;
    if (s->out_idx >= 0) {
        destroy_out(&g_out[s->out_idx]);
        s->out_idx = -1;
    }
    s->used = 0;
    (void)publish_args();
}

static int try_add_device(const char *path, int *changed)
{
    int fd;
    int out_idx;
    struct ig_dev_info info;
    struct ig_class_result cls;
    int selected;

    fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) {
        log_once_failure(path, errno);
        return -1;
    }
    if (is_our_uinput(fd)) {
        close(fd);
        return 0;
    }
    if (load_dev_info(fd, path, &info) != 0) {
        log_once_failure(path, errno);
        close(fd);
        return -1;
    }

    selected = ig_dev_selected(&g_cfg, &info, &cls);
    if (selected) {
        int slot = free_src_slot();
        if (slot < 0) {
            log_msg(IG_LOG_WARN, "device: %s ignored, %d slots in use", path,
                    IG_MAX_DEV);
            close(fd);
            return -1;
        }
        out_idx = free_out_slot();
        if (out_idx < 0) {
            log_msg(IG_LOG_WARN, "device: %s ignored, no uinput slot", path);
            close(fd);
            return -1;
        }
        if (create_uinput_for(fd, &info, &cls, &g_out[out_idx]) != 0) {
            close(fd);
            return -1;
        }
        if (g_out[out_idx].path[0] == '\0') {
            /* Without a published node crosvm has no way to use this device;
             * grabbing anyway would only take input away from Android. */
            log_msg(IG_LOG_ERROR,
                    "device: %s uinput event node not found, not grabbing", path);
            destroy_out(&g_out[out_idx]);
            close(fd);
            return -1;
        }
        if (ioctl(fd, EVIOCGRAB, 1) < 0) {
            int err = errno;
            destroy_out(&g_out[out_idx]);
            log_msg(IG_LOG_ERROR, "device: %s EVIOCGRAB failed: %s", path,
                    strerror(err));
            close(fd);
            return -1;
        }
        memset(&g_src[slot], 0, sizeof(g_src[slot]));
        g_src[slot].used = 1;
        g_src[slot].fd = fd;
        g_src[slot].out_idx = out_idx;
        g_src[slot].kind = cls.kind;
        g_src[slot].info = info;
        snprintf(g_src[slot].path, sizeof(g_src[slot].path), "%s", path);
        snprintf(g_src[slot].name, sizeof(g_src[slot].name), "%s", info.name);
        log_msg(IG_LOG_INFO, "device: %s (%s) grabbed as %s -> %s", path, info.name,
                cls.kind == IG_KIND_KEYBOARD ? "keyboard"
                : cls.kind == IG_KIND_TOUCH  ? (cls.multi_touch ? "multi-touch"
                                                                : "single-touch")
                                             : (cls.absolute_mouse ? "absolute-mouse"
                                                                   : "mouse"),
                g_out[out_idx].path[0] ? g_out[out_idx].path : "(no node)");
        *changed = 1;
        return 0;
    }

    if (ig_dev_has_exit_key(&g_cfg, &info)) {
        int slot = free_src_slot();
        if (slot < 0) {
            close(fd);
            return -1;
        }
        memset(&g_src[slot], 0, sizeof(g_src[slot]));
        g_src[slot].used = 1;
        g_src[slot].fd = fd;
        g_src[slot].out_idx = -1;
        g_src[slot].monitor_only = 1;
        g_src[slot].info = info;
        snprintf(g_src[slot].path, sizeof(g_src[slot].path), "%s", path);
        snprintf(g_src[slot].name, sizeof(g_src[slot].name), "%s", info.name);
        log_msg(IG_LOG_INFO, "device: %s (%s) monitored for exit key (not grabbed)",
                path, info.name);
        return 0;
    }

    close(fd);
    return 0;
}

static void scan_devices(void)
{
    DIR *d;
    struct dirent *de;
    int changed = 0;

    d = opendir("/dev/input");
    if (d == NULL) {
        log_once_failure("/dev/input", errno);
        return;
    }
    while ((de = readdir(d)) != NULL) {
        char path[IG_PATH_MAX];
        if (strncmp(de->d_name, "event", 5) != 0 || strlen(de->d_name) >= 32)
            continue;
        snprintf(path, sizeof(path), "/dev/input/%s", de->d_name);
        if (src_index_by_path(path) >= 0)
            continue;
        if (out_index_by_path(path) >= 0)
            continue;
        (void)try_add_device(path, &changed);
    }
    closedir(d);
    if (changed)
        (void)publish_args();
}

static int publish_args(void)
{
    struct ig_out_desc descs[IG_MAX_DEV];
    char buf[4096];
    char tmp[IG_PATH_MAX + 8];
    int n = 0;
    int i;
    int fd;
    ssize_t written;

    for (i = 0; i < IG_MAX_DEV; i++) {
        if (!g_out[i].used || g_out[i].path[0] == '\0')
            continue;
        memset(&descs[n], 0, sizeof(descs[n]));
        descs[n].type = g_out[i].type;
        descs[n].path = g_out[i].path;
        descs[n].width = g_out[i].width;
        descs[n].height = g_out[i].height;
        n++;
    }
    if (ig_build_args_file(buf, sizeof(buf), descs, n) < 0) {
        log_msg(IG_LOG_ERROR, "args: buffer overflow while formatting %d devices", n);
        return -1;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", g_cfg.args_file);
    fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) {
        log_msg(IG_LOG_ERROR, "args: cannot write %s: %s", tmp, strerror(errno));
        return -1;
    }
    written = write(fd, buf, strlen(buf));
    if (written < 0 || (size_t)written != strlen(buf))
        log_msg(IG_LOG_ERROR, "args: short write to %s: %s", tmp, strerror(errno));
    close(fd);
    (void)chmod(tmp, 0644);
    if (rename(tmp, g_cfg.args_file) != 0) {
        log_msg(IG_LOG_ERROR, "args: cannot rename %s: %s", tmp, strerror(errno));
        return -1;
    }
    log_msg(IG_LOG_INFO, "args: published %d device(s) to %s", n, g_cfg.args_file);
    return 0;
}

/* ---------------------------------------------------------------- events */

static int is_exit_key(int code)
{
    int k;
    for (k = 0; k < g_cfg.n_exit_keys; k++) {
        if (g_cfg.exit_keys[k] == code)
            return 1;
    }
    return 0;
}

static void request_exit(const char *why)
{
    if (g_stop)
        return;
    snprintf(g_exit_reason, sizeof(g_exit_reason), "%s", why);
    g_stop = 1;
}

static int g_key_down[IG_MAX_EXIT_KEYS];
static long long g_key_down_ms[IG_MAX_EXIT_KEYS];

static void track_key(int code, int value)
{
    int k;
    long long now = now_ms();
    for (k = 0; k < g_cfg.n_exit_keys; k++) {
        if (g_cfg.exit_keys[k] != code)
            continue;
        if (value == 1 || value == 2) {
            if (!g_key_down[k]) {
                g_key_down[k] = 1;
                g_key_down_ms[k] = now;
            }
        } else {
            g_key_down[k] = 0;
        }
    }
    if (g_cfg.n_exit_keys == 1 && value == 1 && g_cfg.exit_hold_ms == 0)
        request_exit("exit key");
    if (g_cfg.n_exit_keys > 1 && value == 1) {
        int all = 1;
        for (k = 0; k < g_cfg.n_exit_keys; k++)
            if (!g_key_down[k])
                all = 0;
        if (all)
            request_exit("exit key combo");
    }
}

static void check_exit_hold(void)
{
    long long now = now_ms();

    if (g_cfg.n_exit_keys != 1 || g_key_down[0] == 0)
        return;
    if (g_cfg.exit_hold_ms > 0 && now - g_key_down_ms[0] >= g_cfg.exit_hold_ms) {
        log_msg(IG_LOG_INFO, "exit key %s held for %lld ms",
                ig_key_name(g_cfg.exit_keys[0]) ? ig_key_name(g_cfg.exit_keys[0])
                                                : "?",
                now - g_key_down_ms[0]);
        request_exit("exit key held");
    }
}

static void map_and_forward(struct ig_src *s, const struct input_event *ev)
{
    struct ig_out *o;
    struct input_event out;

    if (s->out_idx < 0 || !g_out[s->out_idx].used)
        return;
    o = &g_out[s->out_idx];
    out = *ev;
    if (out.type == EV_ABS && o->needs_map) {
        int axis = ig_abs_axis((unsigned)out.code);
        if (axis == 1) {
            out.value = ig_scale_abs(out.value, o->src_x_min, o->src_x_max, 0,
                                     o->dst_x_max);
        } else if (axis == 2) {
            out.value = ig_scale_abs(out.value, o->src_y_min, o->src_y_max, 0,
                                     o->dst_y_max);
        }
    }
    uinput_write_event(o->fd, &out);
}

static void read_source(int idx)
{
    struct ig_src *s = &g_src[idx];
    struct input_event evs[64];
    ssize_t n;
    size_t count;
    size_t k;

    n = read(s->fd, evs, sizeof(evs));
    if (n < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
            return;
        remove_src(idx, strerror(errno));
        return;
    }
    if (n == 0) {
        remove_src(idx, "end of stream");
        return;
    }
    count = (size_t)n / sizeof(evs[0]);
    for (k = 0; k < count; k++) {
        const struct input_event *ev = &evs[k];
        if (ev->type == EV_SYN && ev->code != SYN_REPORT)
            continue; /* SYN_DROPPED: do not replay a stale frame marker */
        if (ev->type == EV_KEY && is_exit_key(ev->code)) {
            log_msg(IG_LOG_DEBUG, "exit key event: %s value=%d",
                    ig_key_name(ev->code) ? ig_key_name(ev->code) : "?", ev->value);
            track_key(ev->code, ev->value);
        }
        if (ev->type != EV_SYN && ev->type != EV_KEY && ev->type != EV_REL &&
            ev->type != EV_ABS)
            continue;
        if (s->monitor_only)
            continue;
        map_and_forward(s, ev);
    }
}

/* -------------------------------------------------------------- lifecycle */

static void cleanup_all(void)
{
    int i;

    for (i = 0; i < IG_MAX_DEV; i++) {
        if (g_src[i].used) {
            if (g_src[i].fd >= 0)
                close(g_src[i].fd); /* closing releases EVIOCGRAB */
            g_src[i].fd = -1;
            g_src[i].used = 0;
        }
    }
    for (i = 0; i < IG_MAX_DEV; i++) {
        if (g_out[i].used)
            destroy_out(&g_out[i]);
    }
    /* Leave an empty file: a later crosvm launch must not be pointed at
     * device nodes that no longer exist. */
    {
        int fd = open(g_cfg.args_file, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC,
                      0644);
        if (fd >= 0)
            close(fd);
    }
    log_msg(IG_LOG_INFO, "cleanup done, physical devices released");
}

static void daemonize(void)
{
    pid_t pid = fork();
    if (pid < 0) {
        fprintf(stderr, "inputgrab: fork failed: %s\n", strerror(errno));
        exit(1);
    }
    if (pid > 0)
        _exit(0);
    (void)setsid();
    {
        int devnull = open("/dev/null", O_RDWR);
        if (devnull >= 0) {
            (void)dup2(devnull, STDIN_FILENO);
            (void)dup2(devnull, STDOUT_FILENO);
            (void)dup2(devnull, STDERR_FILENO);
            if (devnull > STDERR_FILENO)
                close(devnull);
        }
    }
    if (chdir("/") != 0) {
        /* keep the inherited cwd; only cosmetic for a daemon */
    }
}

static void install_signals(void)
{
    struct sigaction sa;
    int sigs[] = {SIGINT, SIGTERM, SIGHUP};
    size_t k;

    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    for (k = 0; k < sizeof(sigs) / sizeof(sigs[0]); k++)
        (void)sigaction(sigs[k], &sa, NULL);
    (void)signal(SIGPIPE, SIG_IGN);
}

static void usage(FILE *f)
{
    fprintf(f,
            "inputgrab 1.0 - grab Android input devices and republish them for crosvm\n"
            "\n"
            "Usage: inputgrab [options]\n"
            "  --grab LIST           devices to take: keyboard,touch,mouse (default all)\n"
            "  --exclude NAME[,NAME] skip devices whose name contains NAME (repeatable)\n"
            "  --exit-key SPEC       exit key, e.g. KEY_VOLUMEUP or KEY_LEFTCTRL+KEY_LEFTALT+KEY_F12\n"
            "  --exit-hold-ms N      hold time for a single exit key (default 1500, 0=instant)\n"
            "  --args-file PATH      crosvm argument file (default /data/local/tmp/inputgrab-args)\n"
            "  --log PATH            log file (default /data/local/tmp/inputgrab.log)\n"
            "  --scan-interval-ms N  hotplug rescan interval (default 2000)\n"
            "  --touch-size WxH      rescale touch coordinates to this pixel size\n"
            "  --foreground          stay in the foreground and log to stderr\n"
            "  --help                this text\n");
}

int main(int argc, char **argv)
{
    int i;
    long long next_scan;

    ig_config_init(&g_cfg);
    if (ig_parse_args(argc, argv, &g_cfg) != 0) {
        fprintf(stderr, "inputgrab: %s\n", g_cfg.msg);
        return 2;
    }
    if (g_cfg.help) {
        usage(stdout);
        return 0;
    }

    g_start_ms = now_ms();
    if (!g_cfg.foreground)
        daemonize();

    g_logfd = open(g_cfg.log_file, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC,
                   0644);
    if (g_logfd < 0) {
        fprintf(stderr, "inputgrab: cannot open log %s: %s\n", g_cfg.log_file,
                strerror(errno));
        g_cfg.foreground = 1;
    }
    if (pipe(g_sigpipe) != 0) {
        log_msg(IG_LOG_ERROR, "pipe failed: %s", strerror(errno));
        return 1;
    }
    for (i = 0; i < 2; i++) {
        int fl = fcntl(g_sigpipe[i], F_GETFL, 0);
        if (fl >= 0)
            (void)fcntl(g_sigpipe[i], F_SETFL, fl | O_NONBLOCK);
        (void)fcntl(g_sigpipe[i], F_SETFD, FD_CLOEXEC);
    }
    install_signals();

    log_msg(IG_LOG_INFO,
            "starting: grab=%s%s%s exit=%s n=%d hold=%dms scan=%dms touch=%dx%d",
            (g_cfg.kinds & IG_KIND_KEYBOARD) ? "keyboard " : "",
            (g_cfg.kinds & IG_KIND_TOUCH) ? "touch " : "",
            (g_cfg.kinds & IG_KIND_MOUSE) ? "mouse" : "",
            ig_key_name(g_cfg.exit_keys[0]) ? ig_key_name(g_cfg.exit_keys[0]) : "?",
            g_cfg.n_exit_keys, g_cfg.exit_hold_ms, g_cfg.scan_interval_ms,
            g_cfg.touch_w, g_cfg.touch_h);

    (void)publish_args(); /* start from an empty/current snapshot */
    scan_devices();
    next_scan = now_ms() + g_cfg.scan_interval_ms;

    while (!g_stop) {
        struct pollfd pfds[IG_MAX_DEV + 1];
        int idx_map[IG_MAX_DEV];
        int nfds = 1;
        int r;
        int k;

        memset(pfds, 0, sizeof(pfds));
        pfds[0].fd = g_sigpipe[0];
        pfds[0].events = POLLIN;
        for (k = 0; k < IG_MAX_DEV; k++) {
            if (!g_src[k].used)
                continue;
            pfds[nfds].fd = g_src[k].fd;
            pfds[nfds].events = POLLIN;
            idx_map[nfds] = k;
            nfds++;
        }

        r = poll(pfds, (nfds_t)nfds, 100);
        if (r < 0) {
            if (errno == EINTR)
                continue;
            log_msg(IG_LOG_ERROR, "poll failed: %s", strerror(errno));
            break;
        }
        if (r > 0 && (pfds[0].revents & POLLIN)) {
            char drain[64];
            while (read(g_sigpipe[0], drain, sizeof(drain)) > 0)
                ;
            snprintf(g_exit_reason, sizeof(g_exit_reason), "signal");
            g_stop = 1;
            break;
        }
        for (k = 1; k < nfds; k++) {
            int idx = idx_map[k];
            if (!g_src[idx].used)
                continue;
            if (pfds[k].revents & POLLIN)
                read_source(idx);
            else if (pfds[k].revents & (POLLHUP | POLLERR | POLLNVAL))
                remove_src(idx, "poll hangup");
        }
        if (g_stop)
            break;
        if (now_ms() >= next_scan) {
            scan_devices();
            next_scan = now_ms() + g_cfg.scan_interval_ms;
        }
        check_exit_hold();
    }

    log_msg(IG_LOG_INFO, "exiting (%s) after %lld ms", g_exit_reason,
            now_ms() - g_start_ms);
    cleanup_all();
    if (g_logfd >= 0)
        close(g_logfd);
    return 0;
}
