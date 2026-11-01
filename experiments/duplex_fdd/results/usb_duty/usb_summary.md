# M0 USB capacity

USB contention: up to 221.942/min overflows; see the table

| phase | Pluto | kernel buffers | overflows | /min | start-up (3 s) | RX MS/s | TX duty | max push ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| single_1e6e77_d0.6 | 1e6e77 | 4 | 0 | 0.0 | 0 | 4.998 | 0.6 | 15.3 |
| single_1e6e77_d0.7 | 1e6e77 | 4 | 0 | 0.0 | 0 | 4.999 | 0.7 | 13.42 |
| single_1e6e77_d0.8 | 1e6e77 | 4 | 111 | 221.942 | 0 | 4.781 | 0.777 | 13.12 |
| single_1ef9c6_d0.6 | 1ef9c6 | 4 | 0 | 0.0 | 0 | 5.000 | 0.6 | 15.09 |
| single_1ef9c6_d0.7 | 1ef9c6 | 4 | 0 | 0.0 | 0 | 5.002 | 0.7 | 13.42 |
| single_1ef9c6_d0.8 | 1ef9c6 | 4 | 0 | 0.0 | 0 | 5.002 | 0.793 | 13.07 |

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
