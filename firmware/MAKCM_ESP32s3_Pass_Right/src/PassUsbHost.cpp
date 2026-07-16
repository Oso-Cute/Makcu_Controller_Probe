// PassUsbHost.cpp — see header for design notes.
//
// Enumerates the real controller, snapshots descriptors, claims every
// alt-0 interface, opens its IN endpoints, and forwards URBs to Left
// via IPC. Output is binary IPC frames (not JSON / HID-decoded).

#include "PassUsbHost.h"
#include "pass_ipc.h"
#include "diag.h"
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <esp_log.h>
#include <esp_timer.h>

extern bool ipc_send(uint8_t type, uint8_t ep_addr, uint16_t seq,
                     const uint8_t *payload, uint16_t len);

// Diagnostic log tunneled over IPC (Left forwards to UART0 → CH343 →
// COM3). Right's USB-Serial-JTAG is dead while host mode is active, so
// this is the only visibility into Right's progress.
static void R_LOG_fmt(const char *fmt, ...) {
    char buf[PROBE_MODE ? 240 : 160];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n > 0 && (size_t)n < sizeof(buf)) {
        ipc_send(FRAME_LOG, 0, 0, (const uint8_t *)buf, (uint16_t)n);
    }
}
#define R_LOG(...)  R_LOG_fmt(__VA_ARGS__)

#define PROBE_SCHEMA_VERSION 1
#define PROBE_RIGHT_BUILD_ID "makcu-right-probe-1"

#if PROBE_MODE
static const uint16_t PROBE_HEX_CHUNK = 32;
static const uint16_t PROBE_G7_VID = 0x3537;
static const uint16_t PROBE_G7_PID = 0x1003;

static const char *probe_speed_name(usb_speed_t speed) {
    return speed == USB_SPEED_LOW ? "low" : "full";
}

static const char *probe_ep_type_name(uint8_t attributes) {
    switch (attributes & 0x03) {
    case USB_TRANSFER_TYPE_CTRL:        return "control";
    case USB_TRANSFER_TYPE_ISOCHRONOUS: return "isochronous";
    case USB_TRANSFER_TYPE_BULK:        return "bulk";
    case USB_TRANSFER_TYPE_INTR:        return "interrupt";
    default:                            return "unknown";
    }
}

// Chunk binary objects into parseable ASCII records. A 32-byte chunk keeps
// each FRAME_LOG comfortably below the bounded R_LOG buffer while preserving
// exact descriptor/control bytes for the PC collector.
static void probe_log_blob(const char *record, const char *kind, uint32_t id,
                           const uint8_t *data, uint16_t len) {
    if (!data || len == 0) {
        R_LOG("[PRB] BLOB record=%s kind=%s id=%lu off=0 total=0 hex=-",
              record, kind, (unsigned long)id);
        return;
    }
    for (uint16_t off = 0; off < len; off += PROBE_HEX_CHUNK) {
        uint16_t take = (uint16_t)(len - off);
        if (take > PROBE_HEX_CHUNK) take = PROBE_HEX_CHUNK;
        char hex[PROBE_HEX_CHUNK * 2 + 1];
        for (uint16_t i = 0; i < take; ++i) {
            snprintf(hex + i * 2, sizeof(hex) - i * 2, "%02x", data[off + i]);
        }
        hex[take * 2] = 0;
        R_LOG("[PRB] BLOB record=%s kind=%s id=%lu off=%u total=%u hex=%s",
              record, kind, (unsigned long)id, (unsigned)off,
              (unsigned)len, hex);
    }
}
#endif

static const char *TAG = "PassUsbHost";

PassUsbHost pass_host;
static portMUX_TYPE flow_lock = portMUX_INITIALIZER_UNLOCKED;

void PassUsbHost::probe_hello() const {
#if PROBE_MODE
    R_LOG("[PRB] HELLO schema=%u side=R build=%s probe=%u ipc_baud=%lu",
          (unsigned)PROBE_SCHEMA_VERSION, PROBE_RIGHT_BUILD_ID,
          (unsigned)PROBE_MODE, (unsigned long)PASS_IPC_UART_BAUD);
#else
    (void)this;
#endif
}

bool PassUsbHost::probe_g7_kickstart() {
#if PROBE_MODE
    uint8_t ep = 0;
    bool allowed = false;
    bool start_controller = false;
    portENTER_CRITICAL(&flow_lock);
    allowed = device_connected_ && ready_ &&
              probe_vid_ == PROBE_G7_VID && probe_pid_ == PROBE_G7_PID &&
              probe_g7_out_ep_ != 0;
    if (allowed) {
        ep = probe_g7_out_ep_;
        start_controller = !probe_g7_kick_sent_;
        if (start_controller) probe_g7_kick_sent_ = true;
    }
    portEXIT_CRITICAL(&flow_lock);

    if (!allowed) {
        R_LOG("[PRB] G7_KICK denied vid=%04x pid=%04x ep=%02x connected=%u ready=%u already_sent=%u",
              (unsigned)probe_vid_, (unsigned)probe_pid_,
              (unsigned)probe_g7_out_ep_, (unsigned)device_connected_,
              (unsigned)ready_, (unsigned)probe_g7_kick_sent_);
        return false;
    }

    // A second explicit request is an evidence-only capture arm. It does not
    // send any controller packets a second time; this lets the operator wait
    // for the delayed G7 light, then collect paced raw reports while moving
    // sticks/buttons.
    if (!start_controller) {
        probe_rearm_input_capture();
        R_LOG("[PRB] G7_CAPTURE_ARM ep=%02x limit=128 interval_ms=100",
              (unsigned)ep);
        return true;
    }

    // Exact legacy v0.1 wire packets: identify, power-on, then LED-on.
    // They are deliberately emitted only after an explicit PC request so the
    // normal Xbox passthrough remains entirely host-owned.
    static const uint8_t identify[] = {0x04, 0x20, 0x01, 0x00};
    static const uint8_t power[]    = {0x05, 0x20, 0x02, 0x01, 0x00, 0x00};
    static const uint8_t led[]      = {0x0a, 0x20, 0x03, 0x03, 0x00, 0x01, 0x14, 0x00};
    bool identify_ok = submit_out(ep, identify, sizeof(identify));
    bool power_ok    = submit_out(ep, power, sizeof(power));
    bool led_ok      = submit_out(ep, led, sizeof(led));
    R_LOG("[PRB] G7_KICK ep=%02x identify=%u power=%u led=%u",
          (unsigned)ep, (unsigned)identify_ok, (unsigned)power_ok,
          (unsigned)led_ok);
    return identify_ok && power_ok && led_ok;
#else
    return false;
#endif
}

#if PROBE_MODE
void PassUsbHost::probe_reset() {
    probe_packet_id_ = 0;
    probe_packet_samples_ = 0;
    probe_premount_samples_ = 0;
    probe_out_samples_ = 0;
    probe_out_results_ = 0;
    memset(probe_last_valid_, 0, sizeof(probe_last_valid_));
    memset(probe_last_len_, 0, sizeof(probe_last_len_));
    memset(probe_last_data_, 0, sizeof(probe_last_data_));
    probe_vid_ = 0;
    probe_pid_ = 0;
    probe_g7_out_ep_ = 0;
    probe_g7_kick_sent_ = false;
    probe_g7_capture_armed_ = false;
    memset(probe_last_capture_us_, 0, sizeof(probe_last_capture_us_));
}

void PassUsbHost::probe_rearm_input_capture() {
    probe_packet_samples_ = 0;
    probe_premount_samples_ = 0;
    memset(probe_last_valid_, 0, sizeof(probe_last_valid_));
    memset(probe_last_len_, 0, sizeof(probe_last_len_));
    memset(probe_last_data_, 0, sizeof(probe_last_data_));
    memset(probe_last_capture_us_, 0, sizeof(probe_last_capture_us_));
    probe_g7_capture_armed_ = true;
}

bool PassUsbHost::probe_should_log_in(uint8_t ep_addr, const uint8_t *data,
                                      uint16_t len, bool mounted) {
    if (!data || len == 0 || probe_packet_samples_ >= 128) return false;
    const uint8_t slot = ep_addr & 0x0f;
    // G7 increments a report counter in every 0x20 packet, so ordinary
    // change-only sampling would exhaust the evidence budget in milliseconds.
    // Once the operator explicitly arms post-start capture, retain one exact
    // packet per 100 ms for a useful 12.8-second button/stick window.
    const int64_t now_us = esp_timer_get_time();
    if (probe_g7_capture_armed_ && mounted &&
        probe_last_capture_us_[slot] != 0 &&
        now_us - probe_last_capture_us_[slot] < 100000) {
        return false;
    }
    uint16_t captured = len > sizeof(probe_last_data_[slot])
                      ? (uint16_t)sizeof(probe_last_data_[slot]) : len;
    bool changed = !probe_last_valid_[slot] ||
                   probe_last_len_[slot] != captured ||
                   memcmp(probe_last_data_[slot], data, captured) != 0;
    memcpy(probe_last_data_[slot], data, captured);
    probe_last_len_[slot] = (uint8_t)captured;
    probe_last_valid_[slot] = true;

    if (!mounted) {
        if (probe_premount_samples_ >= 16) return false;
        probe_premount_samples_++;
    } else if (!changed) {
        // Constant-rate HID/XInput pads can report at 1 kHz. Change-only
        // sampling preserves user actions without swamping the 2 Mbps IPC.
        return false;
    }
    if (probe_g7_capture_armed_ && mounted) {
        probe_last_capture_us_[slot] = now_us;
    }
    probe_packet_samples_++;
    return true;
}

void PassUsbHost::probe_log_packet(const char *direction, const char *phase,
                                   uint8_t ep_addr, int status,
                                   const uint8_t *data, uint16_t len) {
    const uint32_t id = __atomic_add_fetch(&probe_packet_id_, 1,
                                           __ATOMIC_RELAXED);
    const int64_t t_us = esp_timer_get_time();
    uint16_t captured = len > 64 ? 64 : len;
    if (!data || captured == 0) {
        R_LOG("[PRB] PKT id=%lu side=R dir=%s phase=%s ep=%02x status=%d t_us=%lld len=%u captured=0 off=0 total=0 hex=-",
              (unsigned long)id, direction, phase, (unsigned)ep_addr, status,
              (long long)t_us, (unsigned)len);
        return;
    }
    for (uint16_t off = 0; off < captured; off += PROBE_HEX_CHUNK) {
        uint16_t take = (uint16_t)(captured - off);
        if (take > PROBE_HEX_CHUNK) take = PROBE_HEX_CHUNK;
        char hex[PROBE_HEX_CHUNK * 2 + 1];
        for (uint16_t i = 0; i < take; ++i) {
            snprintf(hex + i * 2, sizeof(hex) - i * 2, "%02x", data[off + i]);
        }
        hex[take * 2] = 0;
        R_LOG("[PRB] PKT id=%lu side=R dir=%s phase=%s ep=%02x status=%d t_us=%lld len=%u captured=%u off=%u total=%u hex=%s",
              (unsigned long)id, direction, phase, (unsigned)ep_addr, status,
              (long long)t_us, (unsigned)len, (unsigned)captured,
              (unsigned)off, (unsigned)captured, hex);
    }
}
#endif

void PassUsbHost::lib_task(void *arg) {
    auto *self = static_cast<PassUsbHost *>(arg);
    // Register the client only after the lib state machine has stepped
    // at least once — registering too early can miss NEW_DEV events for
    // already-plugged devices.
    while (true) {
        uint32_t flags = 0;
        esp_err_t err = usb_host_lib_handle_events(portMAX_DELAY, &flags);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "usb_host_lib_handle_events err=0x%x", err);
            continue;
        }
        if (self->client_handle_ == nullptr) {
            const usb_host_client_config_t cfg = {
                .is_synchronous = false,
                .max_num_event_msg = 10,
                .async = {
                    .client_event_callback = &PassUsbHost::client_event_cb,
                    .callback_arg = self,
                },
            };
            err = usb_host_client_register(&cfg, &self->client_handle_);
            R_LOG("client_register err=0x%x", err);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "client_register err=0x%x", err);
            } else {
                ESP_LOGI(TAG, "client registered");
                diag_on_client_registered();
            }
        }
    }
    (void)self;
}

void PassUsbHost::client_task(void *arg) {
    auto *self = static_cast<PassUsbHost *>(arg);
    while (true) {
        if (self->client_handle_ == nullptr) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        usb_host_client_handle_events(self->client_handle_, portMAX_DELAY);
    }
}

void PassUsbHost::begin() {
    const usb_host_config_t host_cfg = {
        .skip_phy_setup = false,
        .intr_flags = ESP_INTR_FLAG_LEVEL1,
    };
    esp_err_t err = usb_host_install(&host_cfg);
    diag_on_host_install(err == ESP_OK);
    R_LOG("usb_host_install err=0x%x", err);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "usb_host_install err=0x%x", err);
        return;
    }
    xTaskCreatePinnedToCore(&PassUsbHost::lib_task,    "usb_lib",    8192, this, 5, &lib_task_,    1);
    xTaskCreatePinnedToCore(&PassUsbHost::client_task, "usb_client", 8192, this, 5, &client_task_, 1);
    ESP_LOGI(TAG, "host started");
#if PROBE_MODE
    probe_hello();
#endif
}

void PassUsbHost::client_event_cb(const usb_host_client_event_msg_t *msg, void *arg) {
    auto *self = static_cast<PassUsbHost *>(arg);
    switch (msg->event) {
    case USB_HOST_CLIENT_EVENT_NEW_DEV:
        self->on_new_device(msg->new_dev.address);
        break;
    case USB_HOST_CLIENT_EVENT_DEV_GONE:
        self->on_device_gone();
        break;
    default:
        break;
    }
}

void PassUsbHost::on_new_device(uint8_t address) {
    ESP_LOGI(TAG, "NEW_DEV addr=%u", address);
    R_LOG("NEW_DEV addr=%u", address);
#if PROBE_MODE
    probe_reset();
    R_LOG("[PRB] STATE side=R event=new_device address=%u", (unsigned)address);
#endif
    device_connected_ = true;
    portENTER_CRITICAL(&flow_lock);
    target_mounted_  = false;
    announce_cached_ = false;
    announce_len_    = 0;
    portEXIT_CRITICAL(&flow_lock);
    diag_on_new_dev(address);

    esp_err_t err = usb_host_device_open(client_handle_, address, &device_handle_);
    R_LOG("device_open err=0x%x", err);
#if PROBE_MODE
    R_LOG("[PRB] STATE side=R event=device_open status=%d err=0x%x",
          err == ESP_OK ? 1 : 0, (unsigned)err);
#endif
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "device_open err=0x%x", err);
        return;
    }
    bool rel_ok = fetch_and_relay_descriptors();
    R_LOG("fetch_and_relay_descriptors=%d", (int)rel_ok);
    if (!rel_ok) {
        ESP_LOGE(TAG, "descriptor relay failed — device not usable");
        // Tell Left we're not bringing the device up. Otherwise Left
        // waits forever for a FRAME_DEVICE_READY that never comes.
        ipc_send(FRAME_DEVICE_GONE, 0, 0, nullptr, 0);
        release_all();
        return;
    }
    // MS OS responses are forwarded live via the control-transfer
    // pipeline, so an early probe + cache path isn't needed.
    ipc_send(FRAME_DEVICE_READY, 0, 0, nullptr, 0);
    ready_ = true;
#if PROBE_MODE
    R_LOG("[PRB] STATE side=R event=device_ready");
#endif
    diag_on_device_ready();
}

void PassUsbHost::on_device_gone() {
    ESP_LOGI(TAG, "DEV_GONE");
#if PROBE_MODE
    R_LOG("[PRB] STATE side=R event=device_gone");
#endif
    ready_ = false;
    device_connected_ = false;
    release_all();
    ipc_send(FRAME_DEVICE_GONE, 0, 0, nullptr, 0);
}

bool PassUsbHost::fetch_and_relay_descriptors() {
    const usb_device_desc_t *dev_desc = nullptr;
    esp_err_t e = usb_host_get_device_descriptor(device_handle_, &dev_desc);
    R_LOG("get_device_desc err=0x%x", e);
    if (e != ESP_OK) return false;
    R_LOG("dev VID=%04x PID=%04x cls=%02x/%02x/%02x bcdDev=%04x",
          (unsigned)dev_desc->idVendor, (unsigned)dev_desc->idProduct,
          (unsigned)dev_desc->bDeviceClass, (unsigned)dev_desc->bDeviceSubClass,
          (unsigned)dev_desc->bDeviceProtocol, (unsigned)dev_desc->bcdDevice);
#if PROBE_MODE
    probe_vid_ = dev_desc->idVendor;
    probe_pid_ = dev_desc->idProduct;
    probe_log_blob("desc", "device", 0, (const uint8_t *)dev_desc, 18);
#endif
    ipc_send(FRAME_DESC_DEVICE, 0, 0, (const uint8_t *)dev_desc, 18);

    usb_device_info_t dinfo = {};
    bool dinfo_valid = usb_host_device_info(device_handle_, &dinfo) == ESP_OK;
#if PROBE_MODE
    R_LOG("[PRB] DEVICE vid=%04x pid=%04x bcd=%04x usb=%04x speed=%s address=%u ep0=%u config_value=%u configs=%u class=%02x subclass=%02x protocol=%02x mfg_index=%u product_index=%u serial_index=%u",
          (unsigned)dev_desc->idVendor, (unsigned)dev_desc->idProduct,
          (unsigned)dev_desc->bcdDevice, (unsigned)dev_desc->bcdUSB,
          dinfo_valid ? probe_speed_name(dinfo.speed) : "unknown",
          dinfo_valid ? (unsigned)dinfo.dev_addr : 0,
          dinfo_valid ? (unsigned)dinfo.bMaxPacketSize0
                      : (unsigned)dev_desc->bMaxPacketSize0,
          dinfo_valid ? (unsigned)dinfo.bConfigurationValue : 0,
          (unsigned)dev_desc->bNumConfigurations,
          (unsigned)dev_desc->bDeviceClass,
          (unsigned)dev_desc->bDeviceSubClass,
          (unsigned)dev_desc->bDeviceProtocol,
          (unsigned)dev_desc->iManufacturer,
          (unsigned)dev_desc->iProduct,
          (unsigned)dev_desc->iSerialNumber);
#endif

    const usb_config_desc_t *cfg_desc = nullptr;
    e = usb_host_get_active_config_descriptor(device_handle_, &cfg_desc);
    R_LOG("get_config_desc err=0x%x", e);
    if (e != ESP_OK) return false;
    R_LOG("cfg wTotal=%u nIf=%u",
          (unsigned)cfg_desc->wTotalLength, (unsigned)cfg_desc->bNumInterfaces);
#if PROBE_MODE
    probe_log_blob("desc", "config", 0, (const uint8_t *)cfg_desc,
                   cfg_desc->wTotalLength);
    R_LOG("[PRB] CONFIG value=%u total=%u interfaces=%u attributes=%02x max_power=%u string_index=%u",
          (unsigned)cfg_desc->bConfigurationValue,
          (unsigned)cfg_desc->wTotalLength,
          (unsigned)cfg_desc->bNumInterfaces,
          (unsigned)cfg_desc->bmAttributes,
          (unsigned)cfg_desc->bMaxPower,
          (unsigned)cfg_desc->iConfiguration);
#endif
    ipc_send(FRAME_DESC_CONFIG, 0, 0, (const uint8_t *)cfg_desc, cfg_desc->wTotalLength);

    // Ship every referenced string descriptor: device manufacturer /
    // product / serial, config string, and each interface string.
    uint8_t  idx_set[16] = {0};
    uint8_t  idx_n = 0;
    auto push_idx = [&](uint8_t i) {
        if (!i) return;
        for (uint8_t k = 0; k < idx_n; ++k) if (idx_set[k] == i) return;
        if (idx_n < sizeof(idx_set)) idx_set[idx_n++] = i;
    };
    push_idx(dev_desc->iManufacturer);
    push_idx(dev_desc->iProduct);
    push_idx(dev_desc->iSerialNumber);
    push_idx(cfg_desc->iConfiguration);

    const uint8_t *p   = (const uint8_t *)cfg_desc;
    const uint8_t *end = p + cfg_desc->wTotalLength;
    uint8_t probe_if = 0xff;
    uint8_t probe_alt = 0xff;
    while (p + 2 <= end) {
        uint8_t blen = p[0], btyp = p[1];
        if (!blen) break;
        if (btyp == USB_B_DESCRIPTOR_TYPE_INTERFACE && blen >= 9) {
            const usb_intf_desc_t *ifd = (const usb_intf_desc_t *)p;
            push_idx(ifd->iInterface);
            probe_if = ifd->bInterfaceNumber;
            probe_alt = ifd->bAlternateSetting;
#if PROBE_MODE
            R_LOG("[PRB] IF number=%u alt=%u endpoints=%u class=%02x subclass=%02x protocol=%02x string_index=%u",
                  (unsigned)ifd->bInterfaceNumber,
                  (unsigned)ifd->bAlternateSetting,
                  (unsigned)ifd->bNumEndpoints,
                  (unsigned)ifd->bInterfaceClass,
                  (unsigned)ifd->bInterfaceSubClass,
                  (unsigned)ifd->bInterfaceProtocol,
                  (unsigned)ifd->iInterface);
#endif
        } else if (btyp == USB_B_DESCRIPTOR_TYPE_ENDPOINT && blen >= 7) {
            const usb_ep_desc_t *ed = (const usb_ep_desc_t *)p;
#if PROBE_MODE
            if (probe_vid_ == PROBE_G7_VID && probe_pid_ == PROBE_G7_PID &&
                probe_if == 0 && probe_alt == 0 &&
                !(ed->bEndpointAddress & USB_B_ENDPOINT_ADDRESS_EP_DIR_MASK) &&
                (ed->bmAttributes & 0x03) == 0x03) {
                probe_g7_out_ep_ = ed->bEndpointAddress;
            }
            R_LOG("[PRB] EP interface=%u alt=%u address=%02x direction=%s attributes=%02x type=%s mps=%u interval=%u",
                  (unsigned)probe_if, (unsigned)probe_alt,
                  (unsigned)ed->bEndpointAddress,
                  (ed->bEndpointAddress & USB_B_ENDPOINT_ADDRESS_EP_DIR_MASK)
                      ? "in" : "out",
                  (unsigned)ed->bmAttributes,
                  probe_ep_type_name(ed->bmAttributes),
                  (unsigned)ed->wMaxPacketSize,
                  (unsigned)ed->bInterval);
#endif
        }
        p += blen;
    }

    if (dinfo_valid) {
        auto ship_one = [&](uint8_t idx, const usb_str_desc_t *sd) {
            if (!sd || !idx) return;
            uint8_t body_len = (sd->bLength > 2) ? (sd->bLength - 2) : 0;
            uint8_t buf[129];
            buf[0] = idx;
            if (body_len > sizeof(buf) - 1) body_len = sizeof(buf) - 1;
            // usb_str_desc_t is a union; val[] overlays the whole
            // descriptor starting at bLength. Skip the 2-byte header so
            // we ship only the UTF-16LE body — what Left expects.
            memcpy(&buf[1], sd->val + 2, body_len);
#if PROBE_MODE
            probe_log_blob("desc", "string", idx,
                           (const uint8_t *)sd, sd->bLength);
#endif
            ipc_send(FRAME_DESC_STRING, 0, 0, buf, 1 + body_len);
        };
        ship_one(dev_desc->iManufacturer, dinfo.str_desc_manufacturer);
        ship_one(dev_desc->iProduct,      dinfo.str_desc_product);
        ship_one(dev_desc->iSerialNumber, dinfo.str_desc_serial_num);
    }
    diag_on_descriptors_sent();

    // Claim every interface's alt-0 and open its IN endpoints. (Alt 0 is
    // what SET_CONFIG selects; SET_INTERFACE on alt > 0 is currently
    // forwarded as a control transfer but new endpoints aren't reopened.)
    p = (const uint8_t *)cfg_desc;
    while (p + 2 <= end) {
        uint8_t blen = p[0], btyp = p[1];
        if (!blen) break;
        if (btyp == USB_B_DESCRIPTOR_TYPE_INTERFACE && blen >= 9) {
            const usb_intf_desc_t *ifd = (const usb_intf_desc_t *)p;
            if (ifd->bAlternateSetting == 0 && claimed_if_count_ < PASS_MAX_INTERFACES) {
                esp_err_t ce = usb_host_interface_claim(client_handle_, device_handle_,
                                                       ifd->bInterfaceNumber, 0);
                R_LOG("claim if=%u err=0x%x cls=%02x/%02x/%02x",
                      (unsigned)ifd->bInterfaceNumber, ce,
                      (unsigned)ifd->bInterfaceClass,
                      (unsigned)ifd->bInterfaceSubClass,
                      (unsigned)ifd->bInterfaceProtocol);
                if (ce == ESP_OK) {
                    claimed_ifs_[claimed_if_count_++] = ifd->bInterfaceNumber;
                    open_all_endpoints_for_interface(cfg_desc, ifd->bInterfaceNumber, 0);
                } else {
                    ESP_LOGW(TAG, "interface_claim if=%u err=0x%x",
                             ifd->bInterfaceNumber, ce);
                }
            }
        }
        p += blen;
    }
    return true;
}

// Submit an IN transfer for every IN endpoint belonging to (iface, alt).
// OUT endpoints are allocated on demand in submit_out().
bool PassUsbHost::open_all_endpoints_for_interface(const usb_config_desc_t *cfg,
                                                    uint8_t iface_num,
                                                    uint8_t alt) {
    const uint8_t *p   = (const uint8_t *)cfg;
    const uint8_t *end = p + cfg->wTotalLength;
    bool inside = false;

    while (p + 2 <= end) {
        uint8_t blen = p[0], btyp = p[1];
        if (!blen) break;
        if (btyp == USB_B_DESCRIPTOR_TYPE_INTERFACE && blen >= 9) {
            const usb_intf_desc_t *ifd = (const usb_intf_desc_t *)p;
            inside = (ifd->bInterfaceNumber == iface_num && ifd->bAlternateSetting == alt);
        } else if (btyp == USB_B_DESCRIPTOR_TYPE_ENDPOINT && blen >= 7 && inside) {
            const usb_ep_desc_t *ed = (const usb_ep_desc_t *)p;
            bool is_in = (ed->bEndpointAddress & USB_B_ENDPOINT_ADDRESS_EP_DIR_MASK) != 0;

            if (!is_in) {
                R_LOG("OUT ep=%02x", (unsigned)ed->bEndpointAddress);
            }

            if (is_in && in_transfer_count_ < PASS_MAX_ENDPOINTS) {
                usb_transfer_t *t = nullptr;
                uint16_t mps = ed->wMaxPacketSize ? ed->wMaxPacketSize : 64;
                if (usb_host_transfer_alloc(mps, 0, &t) == ESP_OK) {
                    t->device_handle     = device_handle_;
                    t->bEndpointAddress  = ed->bEndpointAddress;
                    t->num_bytes         = mps;
                    t->callback          = &PassUsbHost::in_xfer_complete;
                    t->context           = this;
                    in_transfers_[in_transfer_count_++] = t;
                    esp_err_t e = usb_host_transfer_submit(t);
                    ESP_LOGI(TAG, "IN submit ep=0x%02x err=0x%x", ed->bEndpointAddress, e);
                    R_LOG("IN open ep=%02x mps=%u submit_err=0x%x",
                          (unsigned)ed->bEndpointAddress, (unsigned)mps, e);
                }
            }
        }
        p += blen;
    }
    return true;
}

void PassUsbHost::set_target_mounted(bool mounted) {
    uint8_t  replay_buf[sizeof(announce_buf_)];
    uint8_t  replay_ep = 0;
    uint16_t replay_len = 0;
    bool     cached = false;

    portENTER_CRITICAL(&flow_lock);
    target_mounted_ = mounted;
    cached = announce_cached_;
    if (mounted && announce_cached_ && announce_len_ > 0) {
        replay_ep  = announce_ep_;
        replay_len = announce_len_;
        memcpy(replay_buf, announce_buf_, replay_len);
    }
    portEXIT_CRITICAL(&flow_lock);

    R_LOG("[EP] TARGET_%s announce_cached=%u",
          mounted ? "MOUNTED" : "RESET", (unsigned)cached);
    if (replay_len > 0) {
        bool sent = ipc_send(FRAME_EP_IN, replay_ep, 0, replay_buf, replay_len);
        R_LOG("[EP] GIP ANNOUNCE_REPLAY ep=%02x len=%u sent=%u",
              (unsigned)replay_ep, (unsigned)replay_len, (unsigned)sent);
#if PROBE_MODE
        R_LOG("[PRB] STATE side=R event=announce_replay ep=%02x len=%u sent=%u",
              (unsigned)replay_ep, (unsigned)replay_len, (unsigned)sent);
#endif
    }
#if PROBE_MODE
    R_LOG("[PRB] STATE side=R event=%s announce_cached=%u announce_ep=%02x announce_len=%u",
          mounted ? "target_mounted" : "target_reset", (unsigned)cached,
          (unsigned)replay_ep, (unsigned)replay_len);
#endif
}

void PassUsbHost::in_xfer_complete(usb_transfer_t *t) {
    auto *self = static_cast<PassUsbHost *>(t->context);
    // If the device is gone (release_all ran) or status is NO_DEVICE /
    // CANCELED, do NOT re-submit — the transfer is about to be freed
    // and a re-submit would race to double-free. ESP-IDF sets every
    // in-flight URB to NO_DEVICE before firing DEV_GONE.
    if (t->status == USB_TRANSFER_STATUS_NO_DEVICE ||
        t->status == USB_TRANSFER_STATUS_CANCELED ||
        !self->device_connected_) {
        return;
    }
    // Diagnostic: log the first ~20 IN completions per endpoint so we can see
    // whether the controller is streaming at all, and with what status/bytes.
    static uint16_t in_dbg = 0;
    if (in_dbg < 20) {
        in_dbg++;
        R_LOG("IN done ep=%02x status=%d nbytes=%u b0=%02x",
              (unsigned)t->bEndpointAddress, (int)t->status,
              (unsigned)t->actual_num_bytes,
              t->actual_num_bytes ? (unsigned)t->data_buffer[0] : 0);
    }
    if (t->status == USB_TRANSFER_STATUS_COMPLETED && t->actual_num_bytes > 0) {
        uint8_t b0 = t->data_buffer[0];
        bool cached_now = false;
        bool forward_now = false;
#if PROBE_MODE
        bool probe_log_now = false;
#endif
        uint16_t cached_len = 0;
        portENTER_CRITICAL(&flow_lock);
        if (b0 == 0x02 && !self->announce_cached_) {
            uint16_t n = (uint16_t)t->actual_num_bytes;
            if (n > sizeof(self->announce_buf_)) n = sizeof(self->announce_buf_);
            memcpy(self->announce_buf_, t->data_buffer, n);
            self->announce_ep_     = t->bEndpointAddress;
            self->announce_len_    = n;
            self->announce_cached_ = true;
            cached_now = true;
            cached_len = n;
        }
        forward_now = self->target_mounted_;
        portEXIT_CRITICAL(&flow_lock);

#if PROBE_MODE
        probe_log_now = self->probe_should_log_in(
            t->bEndpointAddress, t->data_buffer,
            (uint16_t)t->actual_num_bytes, forward_now);
#endif
        if (cached_now) {
            R_LOG("[EP] GIP ANNOUNCE_CACHED ep=%02x len=%u",
                  (unsigned)t->bEndpointAddress,
                  (unsigned)cached_len);
#if PROBE_MODE
            R_LOG("[PRB] STATE side=R event=announce_cached ep=%02x len=%u",
                  (unsigned)t->bEndpointAddress, (unsigned)cached_len);
#endif
        }
        if (forward_now) {
            ipc_send(FRAME_EP_IN, t->bEndpointAddress, 0,
                     t->data_buffer, (uint16_t)t->actual_num_bytes);
        }
#if PROBE_MODE
        if (probe_log_now) {
            self->probe_log_packet("in", forward_now ? "postmount" : "premount",
                                   t->bEndpointAddress, (int)t->status,
                                   t->data_buffer,
                                   (uint16_t)t->actual_num_bytes);
        }
#endif
    }
    usb_host_transfer_submit(t);
}

void PassUsbHost::out_xfer_complete(usb_transfer_t *t) {
#if PROBE_MODE
    auto *self = static_cast<PassUsbHost *>(t->context);
    uint16_t result_index = __atomic_fetch_add(&self->probe_out_results_, 1,
                                                __ATOMIC_RELAXED);
    if (result_index < 128) {
        R_LOG("[PRB] OUT_RESULT side=R ep=%02x status=%d requested=%u actual=%u",
              (unsigned)t->bEndpointAddress, (int)t->status,
              (unsigned)t->num_bytes, (unsigned)t->actual_num_bytes);
    }
#endif
    usb_host_transfer_free(t);
}

void PassUsbHost::control_xfer_complete(usb_transfer_t *t) {
    uint16_t seq = (uint16_t)(uintptr_t)t->context;
    R_LOG("[CTL] done seq=%u status=%d nbytes=%u", seq, (int)t->status,
          (unsigned)t->actual_num_bytes);
    if (t->status == USB_TRANSFER_STATUS_COMPLETED) {
        if (t->actual_num_bytes > 8) {
            ipc_send(FRAME_CTRL_IN_DATA, 0, seq,
                     t->data_buffer + 8, (uint16_t)(t->actual_num_bytes - 8));
        }
        uint8_t status = XFER_OK;
        ipc_send(FRAME_CTRL_STATUS, 0, seq, &status, 1);
    } else {
        uint8_t status = (t->status == USB_TRANSFER_STATUS_STALL) ? XFER_STALL
                       : (t->status == USB_TRANSFER_STATUS_TIMED_OUT) ? XFER_TIMEOUT
                       : XFER_ERROR;
        ipc_send(FRAME_CTRL_STATUS, 0, seq, &status, 1);
    }
#if PROBE_MODE
    // Forward the real completion first; only then serialize diagnostic data.
    uint16_t probe_data_len = t->actual_num_bytes > 8
                            ? (uint16_t)(t->actual_num_bytes - 8) : 0;
    R_LOG("[PRB] CTRL side=R phase=complete seq=%u status=%d data_len=%u",
          (unsigned)seq, (int)t->status, (unsigned)probe_data_len);
    if (probe_data_len > 0) {
        probe_log_blob("control", "in", seq, t->data_buffer + 8,
                       probe_data_len);
    }
#endif
    usb_host_transfer_free(t);
}

bool PassUsbHost::submit_out(uint8_t ep_addr, const uint8_t *data, uint16_t len) {
    if (!device_connected_ || !device_handle_) return false;
    usb_transfer_t *t = nullptr;
    if (usb_host_transfer_alloc(len, 0, &t) != ESP_OK) return false;
    t->device_handle    = device_handle_;
    t->bEndpointAddress = ep_addr;
    t->num_bytes        = len;
    memcpy(t->data_buffer, data, len);
    t->callback = &PassUsbHost::out_xfer_complete;
    t->context  = this;
    if (usb_host_transfer_submit(t) != ESP_OK) {
        usb_host_transfer_free(t);
        return false;
    }
#if PROBE_MODE
    // The physical URB is already queued; trace formatting cannot delay it.
    uint16_t sample_index = __atomic_fetch_add(&probe_out_samples_, 1,
                                               __ATOMIC_RELAXED);
    if (sample_index < 128) {
        probe_log_packet("out", "submit", ep_addr, 0, data, len);
    }
#endif
    return true;
}

bool PassUsbHost::submit_control(const uint8_t setup[8], const uint8_t *data_out,
                                  uint16_t len, uint16_t seq) {
    R_LOG("[CTL] SUBMIT seq=%u setup=%02x %02x %02x %02x %02x %02x %02x %02x dlen=%u",
          seq, setup[0], setup[1], setup[2], setup[3],
          setup[4], setup[5], setup[6], setup[7], (unsigned)len);
    // ANY failure path must emit FRAME_CTRL_STATUS so Left's ctl_pending
    // clears; otherwise every subsequent control stalls.
    auto fail = [&](const char *why, uint8_t status_code) -> bool {
        R_LOG("[CTL] FAIL seq=%u why=%s", seq, why);
        uint8_t s = status_code;
        ipc_send(FRAME_CTRL_STATUS, 0, seq, &s, 1);
        return false;
    };

    if (!device_connected_ || !device_handle_) {
        return fail("NO-DEV", XFER_ERROR);
    }

    const uint16_t wLength = ((uint16_t)setup[6]) | ((uint16_t)setup[7] << 8);
    const bool is_in = (setup[0] & 0x80) != 0;
    const uint16_t data_stage = is_in ? wLength : len;

    // For OUT controls, reject if caller's len doesn't match setup
    // wLength. Prevents ESP-IDF returning ESP_ERR_INVALID_ARG.
    if (!is_in && len != wLength) {
        return fail("OUT-LEN-MISMATCH", XFER_ERROR);
    }

    usb_transfer_t *t = nullptr;
    if (usb_host_transfer_alloc(8 + data_stage, 0, &t) != ESP_OK) {
        return fail("ALLOC-FAIL", XFER_ERROR);
    }

    memcpy(t->data_buffer, setup, 8);
    if (!is_in && len) {
        memcpy(t->data_buffer + 8, data_out, len);
    }
    t->device_handle    = device_handle_;
    t->bEndpointAddress = 0x00;
    t->num_bytes        = 8 + data_stage;
    t->callback         = &PassUsbHost::control_xfer_complete;
    t->context          = (void *)(uintptr_t)seq;

    esp_err_t e = usb_host_transfer_submit_control(client_handle_, t);
    if (e != ESP_OK) {
        R_LOG("[CTL] SUBMIT-FAIL seq=%u err=0x%x", seq, e);
        usb_host_transfer_free(t);
        uint8_t s = XFER_ERROR;
        ipc_send(FRAME_CTRL_STATUS, 0, seq, &s, 1);
        return false;
    }
#if PROBE_MODE
    // Submit first so diagnostic serialization cannot hold off the real
    // device's control transfer.
    const uint16_t wValue = (uint16_t)setup[2] | ((uint16_t)setup[3] << 8);
    const uint16_t wIndex = (uint16_t)setup[4] | ((uint16_t)setup[5] << 8);
    R_LOG("[PRB] CTRL side=R phase=submit seq=%u direction=%s bm=%02x request=%02x value=%04x index=%04x requested=%u out_len=%u",
          (unsigned)seq, is_in ? "in" : "out", (unsigned)setup[0],
          (unsigned)setup[1], (unsigned)wValue, (unsigned)wIndex,
          (unsigned)wLength, (unsigned)len);
    if (!is_in && data_out && len > 0) {
        probe_log_blob("control", "out", seq, data_out, len);
    }
#endif
    return true;
}

void PassUsbHost::release_all() {
    // Mark disconnected FIRST so any in-flight IN completions that fire
    // while we're tearing down bail out of the re-submit path cleanly.
    device_connected_ = false;
#if PROBE_MODE
    R_LOG("[PRB] STATE side=R event=release_all");
#endif
    portENTER_CRITICAL(&flow_lock);
    target_mounted_  = false;
    announce_cached_ = false;
    announce_len_    = 0;
    portEXIT_CRITICAL(&flow_lock);

    // ESP-IDF completes every in-flight URB with status=NO_DEVICE before
    // dispatching DEV_GONE, so they're safe to free here. Halt+flush is
    // belt-and-suspenders against double-free races.
    for (uint8_t i = 0; i < in_transfer_count_; ++i) {
        if (in_transfers_[i]) {
            if (device_handle_) {
                usb_host_endpoint_halt(device_handle_, in_transfers_[i]->bEndpointAddress);
                usb_host_endpoint_flush(device_handle_, in_transfers_[i]->bEndpointAddress);
            }
            usb_host_transfer_free(in_transfers_[i]);
            in_transfers_[i] = nullptr;
        }
    }
    in_transfer_count_ = 0;

    for (uint8_t i = 0; i < claimed_if_count_; ++i) {
        usb_host_interface_release(client_handle_, device_handle_, claimed_ifs_[i]);
        claimed_ifs_[i] = 0;
    }
    claimed_if_count_ = 0;
    if (device_handle_) {
        usb_host_device_close(client_handle_, device_handle_);
        device_handle_ = nullptr;
    }
}
