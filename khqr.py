"""Minimal KHQR (Bakong) payload builder for a personal/individual account.

Builds an EMV merchant-presented QR string with a fixed USD amount.
No external dependencies. Render the returned string with the `qrcode` package.
"""


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def crc16(data: str) -> str:
    crc = 0xFFFF
    for b in data.encode():
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def build_khqr(bakong_id: str, name: str, city: str, amount: float,
               bill_number: str = "") -> str:
    """bakong_id: e.g. 'yourname@aba' (shown in ABA app > Receive money > KHQR)."""
    name = name[:25]
    city = city[:15]
    body = _tlv("00", "01")                       # payload format
    body += _tlv("01", "12")                      # dynamic QR (fixed amount)
    body += _tlv("29", _tlv("00", bakong_id))     # individual account info
    body += _tlv("52", "5999")                    # merchant category
    body += _tlv("53", "840")                     # currency: USD
    body += _tlv("54", f"{amount:.2f}")           # amount
    body += _tlv("58", "KH")                      # country
    body += _tlv("59", name)                      # account holder name
    body += _tlv("60", city)                      # city
    if bill_number:
        body += _tlv("62", _tlv("01", bill_number[:25]))
    body += "6304"
    return body + crc16(body)
