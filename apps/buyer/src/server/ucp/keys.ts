import { createPrivateKey, createPublicKey, KeyObject } from "node:crypto";

export type UcpPublicJwk = Readonly<{
  kid: string;
  kty: "EC";
  crv: "P-256";
  x: string;
  y: string;
  use: "sig";
  alg: "ES256";
}>;

const publicFields = new Set(["alg", "crv", "kid", "kty", "use", "x", "y"]);

const canonicalCoordinate = (value: unknown): value is string => {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(value)) return false;
  const bytes = Buffer.from(value, "base64url");
  return bytes.length === 32 && bytes.toString("base64url") === value;
};

const isP256 = (key: KeyObject): boolean =>
  key.asymmetricKeyType === "ec" && key.asymmetricKeyDetails?.namedCurve === "prime256v1";

export const loadP256PrivateKey = (pem: string): KeyObject => {
  try {
    const key = createPrivateKey(pem);
    if (!isP256(key)) throw new Error();
    return key;
  } catch {
    throw new TypeError("Invalid P-256 private key.");
  }
};

export const exportPublicJwk = (key: KeyObject, keyId: string): UcpPublicJwk => {
  if (!keyId || keyId.length > 255 || !isP256(key)) {
    throw new TypeError("Invalid UCP public key.");
  }
  const exported = createPublicKey(key).export({ format: "jwk" });
  if (!canonicalCoordinate(exported.x) || !canonicalCoordinate(exported.y)) {
    throw new TypeError("Invalid UCP public key.");
  }
  return Object.freeze({
    kid: keyId,
    kty: "EC",
    crv: "P-256",
    x: exported.x,
    y: exported.y,
    use: "sig",
    alg: "ES256",
  });
};

export const importPublicJwk = (jwk: unknown): KeyObject => {
  try {
    if (typeof jwk !== "object" || jwk === null) throw new Error();
    const candidate = jwk as Record<string, unknown>;
    if (
      Object.keys(candidate).some((field) => !publicFields.has(field)) ||
      Object.keys(candidate).length !== publicFields.size ||
      typeof candidate.kid !== "string" ||
      candidate.kid.length < 1 ||
      candidate.kid.length > 255 ||
      candidate.kty !== "EC" ||
      candidate.crv !== "P-256" ||
      candidate.alg !== "ES256" ||
      candidate.use !== "sig" ||
      !canonicalCoordinate(candidate.x) ||
      !canonicalCoordinate(candidate.y)
    ) {
      throw new Error();
    }
    const key = createPublicKey({
      format: "jwk",
      key: { kty: "EC", crv: "P-256", x: candidate.x, y: candidate.y },
    });
    if (!isP256(key)) throw new Error();
    return key;
  } catch {
    throw new TypeError("Invalid UCP public key.");
  }
};
