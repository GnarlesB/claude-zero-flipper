/*
 * Claude Usage - Flipper Zero app
 * Reads "CLU|session=NN|week=NN|reset=XXXX\n" frames over USB serial (CDC)
 * from the companion claude_usage_backend.py and draws them on screen.
 *
 * USB CDC takeover pattern mirrors the official firmware usb_uart_bridge.
 * Build with ufbt (see README). Firmware: official 1.x (API 1.20.x / 88.x).
 */

#include <furi.h>
#include <gui/gui.h>
#include <input/input.h>
#include <furi_hal_usb.h>
#include <furi_hal_usb_cdc.h>
#include <cli/cli_vcp.h>
#include <notification/notification_messages.h>

#define RX_STREAM_SIZE 256
#define LINE_MAX       96
#define VCP_CH         0

typedef struct {
    FuriMutex* mutex;
    int session;       // -1 = unknown
    int week;          // -1 = unknown
    char wreset[16];   // weekly reset (with weekday)
    char sreset[12];   // session reset (time only); "--"/empty = none
    bool have_data;
    bool connected;
} UsageModel;

typedef struct {
    Gui* gui;
    ViewPort* view_port;
    FuriMessageQueue* input_queue;
    FuriStreamBuffer* rx_stream;
    CliVcp* cli_vcp;
    NotificationApp* notif;
    UsageModel model;
    char line[LINE_MAX];
    size_t line_len;
    volatile bool running;
} App;

/* ---- USB CDC callbacks (run in USB/ISR context) ---- */
static void cdc_rx_callback(void* ctx) {
    App* app = ctx;
    uint8_t buf[CDC_DATA_SZ];
    int32_t len = furi_hal_cdc_receive(VCP_CH, buf, CDC_DATA_SZ);
    if(len > 0) {
        furi_stream_buffer_send(app->rx_stream, buf, (size_t)len, 0);
    }
}

static void cdc_state_callback(void* ctx, uint8_t state) {
    App* app = ctx;
    furi_mutex_acquire(app->model.mutex, FuriWaitForever);
    app->model.connected = (state != 0);
    furi_mutex_release(app->model.mutex);
}

static const CdcCallbacks cdc_cb = {
    .tx_ep_callback = NULL,
    .rx_ep_callback = cdc_rx_callback,
    .state_callback = cdc_state_callback,
    .ctrl_line_callback = NULL,
    .config_callback = NULL,
};

/* ---- draw ---- */
static void draw_bar(Canvas* canvas, int x, int y, int w, int h, int pct) {
    canvas_draw_frame(canvas, x, y, w, h);
    if(pct < 0) return;
    if(pct > 100) pct = 100;
    int inner = w - 2;
    int fill = (inner * pct + 50) / 100; // rounded, not truncated
    if(pct > 0 && fill < 1) fill = 1;    // show a sliver for small nonzero %
    if(fill > inner) fill = inner;
    if(fill > 0) canvas_draw_box(canvas, x + 1, y + 1, fill, h - 2);
}

/* Small Claude-style radial burst (12 rays) centered at (cx,cy). */
static void draw_logo(Canvas* canvas, int cx, int cy) {
    static const int8_t rays[12][2] = {
        {6, 0}, {5, 3}, {3, 5}, {0, 6}, {-3, 5}, {-5, 3},
        {-6, 0}, {-5, -3}, {-3, -5}, {0, -6}, {3, -5}, {5, -3},
    };
    for(int i = 0; i < 12; i++) {
        canvas_draw_line(canvas, cx, cy, cx + rays[i][0], cy + rays[i][1]);
    }
}

static void draw_callback(Canvas* canvas, void* ctx) {
    App* app = ctx;
    furi_mutex_acquire(app->model.mutex, FuriWaitForever);
    UsageModel m = app->model; // copy under lock
    furi_mutex_release(app->model.mutex);

    canvas_clear(canvas);
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str(canvas, 2, 10, "CLAUDE ZERO");
    draw_logo(canvas, 120, 8); // small Claude burst in the top-right corner
    canvas_set_font(canvas, FontSecondary);
    // Version, lower-right corner (right-aligned)
    {
        const char* ver = "v1.7";
        int vw = canvas_string_width(canvas, ver);
        canvas_draw_str(canvas, 128 - vw - 1, 63, ver);
    }

    if(!m.have_data) {
        canvas_draw_str(canvas, 2, 30, m.connected ? "Waiting for data..." : "Connect PC backend");
        canvas_draw_str(canvas, 2, 46, "run the PC backend");
        return;
    }

    char buf[32];
    // Session  (label y=23, bar 26..34); session reset right-aligned when > 0%
    if(m.session >= 0) snprintf(buf, sizeof(buf), "Session %d%%", m.session);
    else snprintf(buf, sizeof(buf), "Session --");
    canvas_draw_str(canvas, 2, 23, buf);
    if(m.session > 0 && m.sreset[0] && m.sreset[0] != '-') {
        int w = canvas_string_width(canvas, m.sreset);
        canvas_draw_str(canvas, 126 - w, 23, m.sreset);
    }
    draw_bar(canvas, 2, 26, 124, 8, m.session);

    // Week  (label y=42, bar 45..53)
    if(m.week >= 0) snprintf(buf, sizeof(buf), "Week %d%%", m.week);
    else snprintf(buf, sizeof(buf), "Week --");
    canvas_draw_str(canvas, 2, 42, buf);
    draw_bar(canvas, 2, 45, 124, 8, m.week);

    // Weekly reset on its own line, clear of the week bar
    if(m.wreset[0]) {
        snprintf(buf, sizeof(buf), "resets %s", m.wreset);
        canvas_draw_str(canvas, 2, 63, buf);
    }
}

static void input_callback(InputEvent* event, void* ctx) {
    App* app = ctx;
    furi_message_queue_put(app->input_queue, event, 0);
}

/* ---- parse a complete line: CLU|session=8|week=27|reset=9:10a ---- */
static bool field_int(const char* line, const char* key, int* out) {
    const char* p = strstr(line, key);
    if(!p) return false;
    p += strlen(key);
    if(*p == '-') { *out = -1; return true; } // "--"
    int v = 0;
    bool any = false;
    while(*p >= '0' && *p <= '9') { v = v * 10 + (*p - '0'); p++; any = true; }
    if(any) *out = v;
    return any;
}

static void field_str(const char* line, const char* key, char* dst, size_t dstsz) {
    const char* p = strstr(line, key);
    if(!p) { dst[0] = 0; return; }
    p += strlen(key);
    size_t i = 0;
    while(*p && *p != '|' && *p != '\r' && *p != '\n' && i < dstsz - 1) dst[i++] = *p++;
    dst[i] = 0;
}

static void handle_line(App* app, const char* line) {
    if(strncmp(line, "CLU|", 4) != 0) return;
    int s = -1, w = -1;
    char wr[16], sr[12];
    field_int(line, "session=", &s);
    field_int(line, "week=", &w);
    field_str(line, "wreset=", wr, sizeof(wr));
    field_str(line, "sreset=", sr, sizeof(sr));

    furi_mutex_acquire(app->model.mutex, FuriWaitForever);
    app->model.session = s;
    app->model.week = w;
    strncpy(app->model.wreset, wr, sizeof(app->model.wreset) - 1);
    app->model.wreset[sizeof(app->model.wreset) - 1] = 0;
    strncpy(app->model.sreset, sr, sizeof(app->model.sreset) - 1);
    app->model.sreset[sizeof(app->model.sreset) - 1] = 0;
    app->model.have_data = true;
    furi_mutex_release(app->model.mutex);

    view_port_update(app->view_port);
}

static void feed(App* app, const uint8_t* data, size_t n) {
    for(size_t i = 0; i < n; i++) {
        char c = (char)data[i];
        if(c == '\n') {
            app->line[app->line_len] = 0;
            handle_line(app, app->line);
            app->line_len = 0;
        } else if(c != '\r' && app->line_len < LINE_MAX - 1) {
            app->line[app->line_len++] = c;
        }
    }
}

/* ---- USB CDC take over / restore ---- */
static void usb_take_over(App* app) {
    furi_hal_usb_unlock();
    cli_vcp_disable(app->cli_vcp);
    furi_check(furi_hal_usb_set_config(&usb_cdc_single, NULL) == true);
    furi_hal_cdc_set_callbacks(VCP_CH, (CdcCallbacks*)&cdc_cb, app);
}

static void usb_restore(App* app) {
    furi_hal_cdc_set_callbacks(VCP_CH, NULL, NULL);
    furi_hal_usb_unlock();
    furi_check(furi_hal_usb_set_config(&usb_cdc_single, NULL) == true);
    cli_vcp_enable(app->cli_vcp);
}

int32_t claude_usage_app(void* p) {
    UNUSED(p);
    App* app = malloc(sizeof(App));
    memset(app, 0, sizeof(App));
    app->running = true;
    app->model.session = -1;
    app->model.week = -1;
    app->model.mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->rx_stream = furi_stream_buffer_alloc(RX_STREAM_SIZE, 1);
    app->input_queue = furi_message_queue_alloc(8, sizeof(InputEvent));

    // GUI
    app->gui = furi_record_open(RECORD_GUI);
    app->view_port = view_port_alloc();
    view_port_draw_callback_set(app->view_port, draw_callback, app);
    view_port_input_callback_set(app->view_port, input_callback, app);
    gui_add_view_port(app->gui, app->view_port, GuiLayerFullscreen);

    // Backlight: open the notification service; the main loop re-asserts it
    // every couple seconds so the screen never dims while the app runs.
    app->notif = furi_record_open(RECORD_NOTIFICATION);
    notification_message(app->notif, &sequence_display_backlight_on);

    // USB
    app->cli_vcp = furi_record_open(RECORD_CLI_VCP);
    usb_take_over(app);

    uint8_t buf[64];
    uint32_t last_bl = 0;
    while(app->running) {
        size_t n = furi_stream_buffer_receive(app->rx_stream, buf, sizeof(buf), 50);
        if(n > 0) feed(app, buf, n);

        // Re-assert the backlight every 2s so it never times out.
        uint32_t now = furi_get_tick();
        if(now - last_bl >= 2000) {
            notification_message(app->notif, &sequence_display_backlight_on);
            last_bl = now;
        }

        InputEvent ev;
        while(furi_message_queue_get(app->input_queue, &ev, 0) == FuriStatusOk) {
            if(ev.type == InputTypeShort && ev.key == InputKeyBack) {
                app->running = false;
            }
        }
    }

    // teardown
    usb_restore(app);
    furi_record_close(RECORD_CLI_VCP);

    furi_record_close(RECORD_NOTIFICATION);

    gui_remove_view_port(app->gui, app->view_port);
    view_port_free(app->view_port);
    furi_record_close(RECORD_GUI);

    furi_message_queue_free(app->input_queue);
    furi_stream_buffer_free(app->rx_stream);
    furi_mutex_free(app->model.mutex);
    free(app);
    return 0;
}
