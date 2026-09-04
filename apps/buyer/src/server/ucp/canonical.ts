const encoder = new TextEncoder();

const keyOrder = (left: string, right: string): number => {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0) as number);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0) as number);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftPoints[index] - rightPoints[index];
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
};

const normalize = (value: unknown, ancestors: WeakSet<object>): unknown => {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) throw new TypeError("Value is not canonical JSON.");
    return value;
  }
  if (typeof value !== "object") throw new TypeError("Value is not canonical JSON.");
  if (ancestors.has(value)) throw new TypeError("Value is not canonical JSON.");
  ancestors.add(value);
  try {
    if (Array.isArray(value)) return value.map((item) => normalize(item, ancestors));
    const prototype = Object.getPrototypeOf(value) as object | null;
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError("Value is not canonical JSON.");
    }
    const record = value as Record<string, unknown>;
    const result: Record<string, unknown> = {};
    for (const key of Object.keys(record).sort(keyOrder)) {
      result[key] = normalize(record[key], ancestors);
    }
    return result;
  } finally {
    ancestors.delete(value);
  }
};

export const canonicalJson = (value: unknown): Uint8Array => {
  try {
    return encoder.encode(JSON.stringify(normalize(value, new WeakSet())));
  } catch (error) {
    if (error instanceof TypeError && error.message.includes("canonical JSON")) throw error;
    throw new TypeError("Value is not canonical JSON.");
  }
};
