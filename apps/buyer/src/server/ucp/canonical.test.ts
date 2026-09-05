import { describe, expect, it } from "vitest";

import { canonicalJson } from "./canonical";

const text = (value: Uint8Array): string => new TextDecoder().decode(value);

describe("canonicalJson", () => {
  it("sorts every object while preserving array order and Unicode", () => {
    expect(
      text(canonicalJson({ z: [{ b: "काला", a: 2 }], a: { y: false, x: null } })),
    ).toBe('{"a":{"x":null,"y":false},"z":[{"a":2,"b":"काला"}]}');
  });

  it.each([
    [Number.NaN],
    [Number.POSITIVE_INFINITY],
    [Number.MAX_SAFE_INTEGER + 1],
    [BigInt(1)],
    [undefined],
    [new Date("2026-09-04T00:00:00Z")],
    [{ value: undefined }],
    [[undefined]],
  ])("rejects a value JSON could silently change: %s", (value) => {
    expect(() => canonicalJson(value)).toThrowError("canonical JSON");
  });

  it("rejects cyclic objects", () => {
    const value: Record<string, unknown> = {};
    value.self = value;

    expect(() => canonicalJson(value)).toThrowError("canonical JSON");
  });
});
