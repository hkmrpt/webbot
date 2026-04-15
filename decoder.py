import struct
from datetime import datetime


class ZerodhaDataDecoder:
    EXCHANGE_MAP = {
        "nse": 1, "nfo": 2, "cds": 3, "bse": 4,
        "bfo": 5, "bsecds": 6, "mcx": 7, "mcxsx": 8, "indices": 9
    }

    @staticmethod
    def _unpack_int(b, s, e, fmt="I"):
        return struct.unpack(">" + fmt, b[s:e])[0]

    @classmethod
    def _split_packets(cls, data):
        if len(data) < 2:
            return []
        n = struct.unpack(">H", data[:2])[0]
        packets, pos = [], 2
        for _ in range(n):
            length = struct.unpack(">H", data[pos:pos + 2])[0]
            packets.append(data[pos + 2: pos + 2 + length])
            pos += 2 + length
        return packets

    @classmethod
    def decode(cls, data):
        ticks = []
        for pkt in cls._split_packets(data):
            if not pkt or len(pkt) < 8:
                continue
            token   = cls._unpack_int(pkt, 0, 4)
            seg     = token & 0xff
            divisor = 10_000_000.0 if seg == cls.EXCHANGE_MAP["cds"] else 100.0
            tick = {
                "instrument_token": token,
                "last_price": cls._unpack_int(pkt, 4, 8) / divisor,
            }
            # Extended packet — grab OHLC if available
            if len(pkt) >= 44:
                tick.update({
                    "volume":        cls._unpack_int(pkt, 16, 20),
                    "ohlc": {
                        "open":  cls._unpack_int(pkt, 28, 32) / divisor,
                        "high":  cls._unpack_int(pkt, 32, 36) / divisor,
                        "low":   cls._unpack_int(pkt, 36, 40) / divisor,
                        "close": cls._unpack_int(pkt, 40, 44) / divisor,
                    }
                })
            ticks.append(tick)
        return ticks
