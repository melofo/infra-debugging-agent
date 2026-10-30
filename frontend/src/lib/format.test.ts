import { describe, expect, it } from "vitest";
import { formatDuration, formatUtc, timeAgo } from "./format";

const NOW = new Date("2026-10-30T12:00:00Z").getTime();
const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString();

describe("timeAgo", () => {
  it.each([
    [0, "just now"],
    [4, "just now"],
    [30, "30s ago"],
    [59, "59s ago"],
    [60, "1 min ago"],
    [59 * 60, "59 min ago"],
    [3600, "1 h ago"],
    [23 * 3600, "23 h ago"],
    [24 * 3600, "1 d ago"],
    [3 * 24 * 3600, "3 d ago"],
  ])("%i seconds ago -> %s", (seconds, expected) => {
    expect(timeAgo(ago(seconds), NOW)).toBe(expected);
  });

  it("never shows negative time when the server clock is slightly ahead of ours", () => {
    expect(timeAgo(new Date(NOW + 5000).toISOString(), NOW)).toBe("just now");
  });
});

describe("formatDuration", () => {
  it("formats milliseconds as seconds with one decimal", () => {
    expect(formatDuration(4130)).toBe("4.1s");
    expect(formatDuration(0)).toBe("0.0s");
  });
});

describe("formatUtc", () => {
  it("formats an ISO timestamp in UTC", () => {
    expect(formatUtc("2026-10-20T14:02:11Z")).toBe("2026-10-20 14:02:11 UTC");
  });
});
