// PassUsbHost — stripped ESP-IDF usb_host wrapper for the passthrough.
// Enumerates the attached device, snapshots descriptors, opens IN/OUT
// endpoints, and forwards every URB to hooks exported by main.cpp (which
// frame and ship them over IPC to Left). No HID decoding, no GIP
// handshake generation — passes the wire data through unchanged.

#pragma once

#include <Arduino.h>
#include <usb/usb_host.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#define PASS_MAX_ENDPOINTS   8
#define PASS_MAX_INTERFACES  4

class PassUsbHost {
public:
    void begin();

    // OUT transfer on a previously-opened endpoint. Caller keeps the
    // buffer; we copy it into a fresh URB allocated per call.
    bool submit_out(uint8_t ep_addr, const uint8_t *data, uint16_t len);

    // Control transfer derived from an 8-byte SETUP packet. For DATA-OUT
    // transfers, `data_out` carries the host's DATA stage. For DATA-IN,
    // the completion callback ships FRAME_CTRL_IN_DATA + FRAME_CTRL_STATUS
    // back over IPC.
    bool submit_control(const uint8_t setup[8], const uint8_t *data_out, uint16_t len, uint16_t seq);

    // Left reports when the target Xbox/PC has completed SET_CONFIGURATION
    // or reset. Real-device IN traffic is held until mounted; a cached GIP
    // announce is replayed on each mount so the target owns the handshake.
    void set_target_mounted(bool mounted);

    bool is_ready() const { return ready_; }

private:
    usb_host_client_handle_t client_handle_   = nullptr;
    usb_device_handle_t      device_handle_   = nullptr;
    bool                     device_connected_ = false;
    bool                     ready_            = false;
    TaskHandle_t             lib_task_     = nullptr;
    TaskHandle_t             client_task_  = nullptr;

    usb_transfer_t *in_transfers_[PASS_MAX_ENDPOINTS]  = {nullptr};
    uint8_t         in_transfer_count_                 = 0;
    uint8_t         claimed_ifs_[PASS_MAX_INTERFACES]  = {0};
    uint8_t         claimed_if_count_                  = 0;

    static void lib_task(void *arg);
    static void client_task(void *arg);

    static void client_event_cb(const usb_host_client_event_msg_t *msg, void *arg);
    void        on_new_device(uint8_t address);
    void        on_device_gone();

    static void in_xfer_complete(usb_transfer_t *t);
    static void out_xfer_complete(usb_transfer_t *t);
    static void control_xfer_complete(usb_transfer_t *t);

    bool fetch_and_relay_descriptors();
    bool open_all_endpoints_for_interface(const usb_config_desc_t *cfg,
                                          uint8_t bInterfaceNumber,
                                          uint8_t bAlternateSetting);

    // A GIP controller announces (command 0x02) as soon as Right enumerates
    // it, which can be before Left is visible to the Xbox. Cache that exact
    // packet and replay it only after Left reports target mount. Do not
    // synthesize identify/power/LED commands here: target OUT traffic must
    // drive the real device's handshake end-to-end.
    bool     target_mounted_ = false;
    bool     announce_cached_ = false;
    uint8_t  announce_ep_ = 0x82;
    uint16_t announce_len_ = 0;
    uint8_t  announce_buf_[64] = {0};

    void release_all();
};

extern PassUsbHost pass_host;
