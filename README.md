# M11ShutterCount

Read the shutter count and usage counters of a Leica M11 over USB PTP.

> **Unofficial.** Not affiliated with or endorsed by Leica Camera AG. "Leica" and "M11" are trademarks of Leica Camera AG. Use at your own risk.

## Requirements

- Python 3
- [pyusb](https://pypi.org/project/pyusb/) and libusb:

  ```sh
  pip install pyusb
  brew install libusb
  ```

## Usage

1. Turn the camera on and connect it to your computer with a USB-C data cable.
2. Close Image Capture, Photos, Lightroom or any other app that uses the camera.
3. Run:

   ```sh
   python3 m11_shutter_count.py
   ```

On macOS, the system camera service (`ptpcamerad`) takes the camera as soon as it connects. The script stops that service so it can read the camera, and macOS restarts it when needed.

| Option | Description |
|---|---|
| `--json` | Print the camera's raw JSON reply |
| `-v`, `--verbose` | Hex-dump every PTP message to stderr |

## Example output

```
Leica Camera AG LEICA M11
  Firmware:  2.6.0
  Serial:    000000000566XXXX

  Shutter Actions Count                37,123
  Operating Time Counter            3,456,763
  Switch On Counter                     6,447
  Switch On Power Switch Counter        6,233
  Switch On Power Save Counter            214
  Switch Off Counter                    7,589
  Switch Off Power Switch Counter       6,166
  Switch Off Power Save Counter         1,423
  Switch Off Other Counter                  0
  Switch Off Breakdown Counter             66
```

## Compatibility

Tested only on a Leica M11 with firmware 2.6.0 via an arm MacBook running macOS 27.

## License

[MIT](LICENSE)
