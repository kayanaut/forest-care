"""Health checks for rover data: the same rules for a recorded bag (`fc_gateway inspect`) and for
live topics before driving (`ros2 run forestcare_gateway preflight`).

Each check gives OK / WARN / FAIL with a sentence an operator can act on.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from .messages import NS, gnss_from_msg, image_size, stamp_ns
from .recorder import MIN_WALL_CLOCK_NS

# minimum useful rates (Hz) per role; roles without an entry are only reported
MIN_RATE = {"gnss": 1.0, "camera": 1.0, "odom": 10.0, "imu": 20.0, "lidar": 5.0}
REQUIRED = ("gnss", "camera")


@dataclass
class TopicStats:
    role: str
    topic: str
    count: int = 0
    first_ns: int | None = None
    last_ns: int | None = None
    offsets_ms: list = field(default_factory=list)       # arrival - header stamp
    stamps_before_2020: int = 0
    extra: Counter = field(default_factory=Counter)
    sigmas: list = field(default_factory=list)
    sizes: set = field(default_factory=set)

    def add(self, msgtype: str, msg, arrival_ns: int) -> None:
        self.count += 1
        self.first_ns = arrival_ns if self.first_ns is None else min(self.first_ns, arrival_ns)
        self.last_ns = arrival_ns if self.last_ns is None else max(self.last_ns, arrival_ns)
        header = getattr(msg, "header", None)
        if header is not None:
            s = stamp_ns(header.stamp)
            if s < MIN_WALL_CLOCK_NS:
                self.stamps_before_2020 += 1
            elif self.count % 5 == 1:
                self.offsets_ms.append((arrival_ns - s) / 1e6)
        if self.role in ("gnss", "gnss_rtk"):
            fix = gnss_from_msg(msg)
            self.extra[f"status {fix.status}"] += 1
            self.extra["valid"] += fix.valid
            if fix.sigma_m:
                self.sigmas.append(fix.sigma_m)
            elif fix.valid:
                self.extra["no covariance"] += 1
        elif self.role == "camera" and msgtype.endswith("CompressedImage") and len(self.sizes) < 5:
            self.sizes.add(image_size(bytes(msg.data)))
        elif self.role == "imu" and float(msg.orientation_covariance[0]) == -1.0:
            self.extra["no orientation"] += 1

    @property
    def rate_hz(self) -> float:
        if self.count < 2 or self.first_ns == self.last_ns:
            return 0.0
        return (self.count - 1) / ((self.last_ns - self.first_ns) / NS)


def checks(stats: dict[str, TopicStats], cfg: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for role, topic in cfg["topics"].items():
        if not topic:
            continue
        st = stats.get(role)
        if st is None or st.count == 0:
            level = "FAIL" if role in REQUIRED else ("WARN" if role in MIN_RATE else "INFO")
            out.append((level, f"{role}: no messages on {topic}"))
            continue
        rate = st.rate_hz
        want = MIN_RATE.get(role)
        level = "OK" if want is None or rate >= want else "WARN"
        out.append((level, f"{role}: {st.count} messages on {topic}, {rate:.1f} Hz" + (f" (want >= {want:g})" if level == "WARN" else "")))
        if st.stamps_before_2020:
            out.append(("FAIL", f"{role}: {st.stamps_before_2020} header stamps are not wall-clock time (clock not set, or sim time)"))
        if st.offsets_ms:
            med = statistics.median(st.offsets_ms)
            if abs(med) > 500:
                out.append(("WARN", f"{role}: stamps are {med / 1000:+.2f} s from arrival time; clocks not synchronised?"))
        if role in ("gnss", "gnss_rtk"):
            valid = st.extra["valid"] / st.count
            level = "OK" if valid > 0.9 else ("WARN" if valid > 0.5 else "FAIL")
            statuses = ", ".join(f"{k}: {v}" for k, v in sorted(st.extra.items()) if k.startswith("status"))
            out.append((level, f"{role}: {valid:.0%} valid fixes ({statuses})"))
            if st.sigmas:
                s = sorted(st.sigmas)
                med, p90 = statistics.median(s), s[int(0.9 * (len(s) - 1))]
                level = "OK" if med <= 3 else "WARN"
                out.append((level, f"{role}: reported accuracy median {med:.2f} m, 90% {p90:.2f} m (1 sigma per axis)"))
            if st.extra["no covariance"]:
                out.append(("WARN", f"{role}: {st.extra['no covariance']} fixes without covariance; "
                                    f"{cfg['gnss']['assumed_sigma_m']} m will be assumed"))
        if role == "camera" and st.sizes:
            sizes = ", ".join(f"{w}x{h}" for w, h in st.sizes if w)
            out.append(("OK" if sizes else "WARN", f"camera: frame size {sizes or 'unreadable'}"))
        if role == "imu" and st.extra["no orientation"]:
            out.append(("INFO", "imu: no orientation (gyro/accelerometer only); fine for the GNSS+IMU estimator"))
    return out


def render(lines: list[tuple[str, str]], title: str) -> tuple[str, bool]:
    ok = not any(level == "FAIL" for level, _ in lines)
    text = "\n".join([title, *(f"  [{level:4s}] {msg}" for level, msg in lines),
                      "  => " + ("ready" if ok else "NOT ready: fix the FAIL lines first")])
    return text, ok


def inspect_bag(bag, cfg: dict) -> dict:
    from .bagio import bag_summary, read_messages

    summary = bag_summary(bag)
    roles = {topic: role for role, topic in cfg["topics"].items() if topic}
    stats = {role: TopicStats(role, topic) for topic, role in roles.items()}
    for topic, msgtype, t, msg in read_messages(bag, list(roles)):
        stats[roles[topic]].add(msgtype, msg, t)
    lines = checks(stats, cfg)
    extra = sorted(set(summary["topics"]) - set(roles))
    if extra:
        lines.append(("INFO", f"also recorded (kept in the bag, not used by the gateway): {', '.join(extra)}"))
    text, ok = render(lines, f"{summary['name']}: {summary['duration_s']} s, {sum(t['messages'] for t in summary['topics'].values())} messages")
    return {"ok": ok, "text": text, "checks": lines, "summary": summary}
