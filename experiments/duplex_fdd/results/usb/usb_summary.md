# M0 USB capacity

USB contention: up to 223.988/min overflows; see the table

| phase | Pluto | kernel buffers | overflows | /min | start-up (3 s) | RX MS/s | TX duty | max push ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| single_1e6e77_d0 | 1e6e77 | 4 | 0 | 0.0 | 0 | 4.999 | None | None |
| single_1e6e77_d0.5 | 1e6e77 | 4 | 0 | 0.0 | 0 | 5.000 | 0.5 | 22.11 |
| single_1e6e77_d0.9 | 1e6e77 | 4 | 224 | 223.988 | 0 | 4.775 | 0.777 | 14.13 |
| single_1ef9c6_d0 | 1ef9c6 | 4 | 0 | 0.0 | 0 | 4.999 | None | None |
| single_1ef9c6_d0.5 | 1ef9c6 | 4 | 0 | 0.0 | 0 | 5.000 | 0.5 | 18.54 |
| single_1ef9c6_d0.9 | 1ef9c6 | 4 | 0 | 0.0 | 0 | 5.000 | 0.793 | 13.91 |
| both_d0.9_kb4 | 1e6e77 | 4 | 183 | 182.98 | 0 | 4.896 | 0.78 | 19.71 |
| both_d0.9_kb4 | 1ef9c6 | 4 | 0 | 0.0 | 0 | 5.001 | 0.772 | 15.16 |
| both_d0.9_kb8 | 1e6e77 | 8 | 177 | 176.966 | 0 | 4.896 | 0.776 | 18.96 |
| both_d0.9_kb8 | 1ef9c6 | 8 | 0 | 0.0 | 0 | 5.001 | 0.769 | 14.83 |

Double open: `[{"kind": "double_open", "serial": "104473541196000419000c00f2ff1e6e77", "second_open": "failed", "error": "[Errno 16] cannot open usb:3.10.5: Device or resource busy"}]`

```
/:  Bus 04.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/9p, 20000M/x2
    |__ Port 9: Dev 2, If 0, Class=Hub, Driver=hub/4p, 5000M
        |__ Port 1: Dev 3, If 0, Class=Hub, Driver=hub/4p, 5000M
/:  Bus 03.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/14p, 480M
    |__ Port 2: Dev 2, If 0, Class=Vendor Specific Class, Driver=, 12M
    |__ Port 2: Dev 2, If 2, Class=Human Interface Device, Driver=usbhid, 12M
    |__ Port 3: Dev 12, If 0, Class=Communications, Driver=rndis_host, 480M
    |__ Port 3: Dev 12, If 5, Class=Communications, Driver=, 480M
    |__ Port 3: Dev 12, If 3, Class=Communications, Driver=cdc_acm, 480M
    |__ Port 3: Dev 12, If 1, Class=CDC Data, Driver=rndis_host, 480M
    |__ Port 3: Dev 12, If 4, Class=CDC Data, Driver=cdc_acm, 480M
    |__ Port 3: Dev 12, If 2, Class=Mass Storage, Driver=usb-storage, 480M
    |__ Port 5: Dev 3, If 2, Class=Audio, Driver=snd-usb-audio, 480M
    |__ Port 5: Dev 3, If 0, Class=Video, Driver=uvcvideo, 480M
    |__ Port 5: Dev 3, If 3, Class=Audio, Driver=snd-usb-audio, 480M
    |__ Port 5: Dev 3, If 1, Class=Video, Driver=uvcvideo, 480M
    |__ Port 10: Dev 4, If 0, Class=Hub, Driver=hub/4p, 480M
        |__ Port 1: Dev 6, If 0, Class=Hub, Driver=hub/4p, 480M
            |__ Port 3: Dev 8, If 3, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 3: Dev 8, If 1, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 3: Dev 8, If 2, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 3: Dev 8, If 0, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 5, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 3, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 1, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 4, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 2, Class=Human Interface Device, Driver=usbhid, 12M
            |__ Port 4: Dev 9, If 0, Class=Human Interface Device, Driver=usbhid, 12M
        |__ Port 4: Dev 10, If 1, Class=CDC Data, Driver=rndis_host, 480M
        |__ Port 4: Dev 10, If 4, Class=CDC Data, Driver=cdc_acm, 480M
        |__ Port 4: Dev 10, If 2, Class=Mass Storage, Driver=usb-storage, 480M
        |__ Port 4: Dev 10, If 0, Class=Communications, Driver=rndis_host, 480M
        |__ Port 4: Dev 10, If 5, Class=Communications, Driver=, 480M
        |__ Port 4: Dev 10, If 3, Class=Communications, Driver=cdc_acm, 480M
        |__ Port 2: Dev 7, If 0, Class=Wireless, Driver=btusb, 12M
        |__ Port 2: Dev 7, If 1, Class=Wireless, Driver=btusb, 12M
    |__ Port 14: Dev 5, If 2, Class=Wireless, Driver=, 480M
    |__ Port 14: Dev 5, If 0, Class=Wireless, Driver=btusb, 480M
    |__ Port 14: Dev 5, If 1, Class=Wireless, Driver=btusb, 480M
/:  Bus 02.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/1p, 20000M/x2
/:  Bus 01.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/1p, 480M
```
