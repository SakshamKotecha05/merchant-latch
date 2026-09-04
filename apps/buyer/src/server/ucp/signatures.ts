import {
  createHash,
  KeyObject,
  sign as signBytes,
  timingSafeEqual,
  verify as verifyBytes,
} from "node:crypto";

export class UcpSignatureError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("UCP message authentication failed.");
    this.name = "UcpSignatureError";
    this.code = code;
  }
}

type HeaderSource = Headers | Readonly<Record<string, string>>;

export type SignRequestInput = Readonly<{
  method: string;
  url: URL;
  headers: HeaderSource;
  body: Uint8Array;
  privateKey: KeyObject;
  keyId: string;
  created: number;
  expires: number;
  nonce: string;
}>;

export type VerifyResponseInput = Readonly<{
  status: number;
  headers: HeaderSource;
  body: Uint8Array;
  publicKey: KeyObject;
  expectedKeyId: string;
  now: number;
}>;

export type SignedRequestHeaders = Readonly<{
  "Content-Digest": string;
  "Signature-Input": string;
  Signature: string;
}>;

const headerValue = (headers: HeaderSource, name: string): string | undefined => {
  if (headers instanceof Headers) return headers.get(name) ?? undefined;
  const match = Object.entries(headers).find(([key]) => key.toLowerCase() === name.toLowerCase());
  return match?.[1];
};

const safeParameter = (value: string): boolean =>
  value.length > 0 && value.length <= 255 && /^[A-Za-z0-9._:/-]+$/.test(value);

const validTimes = (created: number, expires: number): boolean =>
  Number.isSafeInteger(created) &&
  Number.isSafeInteger(expires) &&
  created >= 0 &&
  expires > created &&
  expires - created <= 300;

const digestBytes = (body: Uint8Array): Buffer => createHash("sha256").update(body).digest();

export const contentDigest = (body: Uint8Array): string =>
  `sha-256=:${digestBytes(body).toString("base64")}:`;

export const verifyContentDigest = (body: Uint8Array, header: string | undefined): void => {
  const match = /^sha-256=:([A-Za-z0-9+/]{43}=):$/.exec(header ?? "");
  if (match === null) throw new UcpSignatureError("digest_invalid");
  const supplied = Buffer.from(match[1], "base64");
  if (
    supplied.length !== 32 ||
    supplied.toString("base64") !== match[1] ||
    !timingSafeEqual(supplied, digestBytes(body))
  ) {
    throw new UcpSignatureError("digest_invalid");
  }
};

const requestComponents = (input: SignRequestInput): readonly string[] => {
  const method = input.method.toUpperCase();
  if (!/^[A-Z]+$/.test(method)) throw new UcpSignatureError("components_invalid");
  const components = ["@method", "@authority", "@path"];
  if (input.url.search) components.push("@query");
  const agent = headerValue(input.headers, "ucp-agent");
  if (!agent || /[\r\n]/.test(agent)) throw new UcpSignatureError("components_invalid");
  components.push("ucp-agent");
  const idempotency = headerValue(input.headers, "idempotency-key");
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && !idempotency) {
    throw new UcpSignatureError("idempotency_missing");
  }
  if (idempotency) {
    if (/[^\x21-\x7e]/.test(idempotency)) throw new UcpSignatureError("components_invalid");
    components.push("idempotency-key");
  }
  if (input.body.length > 0) {
    const contentType = headerValue(input.headers, "content-type");
    if (!contentType || /[\r\n]/.test(contentType)) {
      throw new UcpSignatureError("components_invalid");
    }
    components.push("content-digest", "content-type");
  }
  return components;
};

const requestComponentValue = (
  component: string,
  input: SignRequestInput,
  digest: string,
): string => {
  if (component === "@method") return input.method.toUpperCase();
  if (component === "@authority") return input.url.host;
  if (component === "@path") return input.url.pathname;
  if (component === "@query") return input.url.search;
  if (component === "content-digest") return digest;
  const value = headerValue(input.headers, component);
  if (value === undefined) throw new UcpSignatureError("components_invalid");
  return value;
};

export const signUcpRequest = (input: SignRequestInput): SignedRequestHeaders => {
  if (
    input.url.protocol !== "https:" ||
    input.url.username ||
    input.url.password ||
    input.url.hash ||
    !safeParameter(input.keyId) ||
    !safeParameter(input.nonce) ||
    !validTimes(input.created, input.expires) ||
    input.privateKey.type !== "private" ||
    input.privateKey.asymmetricKeyType !== "ec" ||
    input.privateKey.asymmetricKeyDetails?.namedCurve !== "prime256v1"
  ) {
    throw new UcpSignatureError("signature_input_invalid");
  }
  const components = requestComponents(input);
  const digest = contentDigest(input.body);
  const parameters = `(${components.map((value) => `"${value}"`).join(" ")});created=${input.created};keyid="${input.keyId}";expires=${input.expires};nonce="${input.nonce}"`;
  const base = [
    ...components.map(
      (component) => `"${component}": ${requestComponentValue(component, input, digest)}`,
    ),
    `"@signature-params": ${parameters}`,
  ].join("\n");
  const signature = signBytes("sha256", Buffer.from(base), {
    key: input.privateKey,
    dsaEncoding: "ieee-p1363",
  });
  if (signature.length !== 64) throw new UcpSignatureError("signature_invalid");
  return Object.freeze({
    "Content-Digest": digest,
    "Signature-Input": `sig1=${parameters}`,
    Signature: `sig1=:${signature.toString("base64")}:`,
  });
};

const RESPONSE_WITH_BODY = '("@status" "content-digest" "content-type")';
const RESPONSE_WITHOUT_BODY = '("@status")';
const RESPONSE_INPUT =
  /^sig1=(\("@status"(?: "content-digest" "content-type")?\));created=(\d+);keyid="([A-Za-z0-9._:/-]{1,255})";expires=(\d+)$/;

export const verifyUcpResponse = (input: VerifyResponseInput): void => {
  const digest = headerValue(input.headers, "content-digest");
  const contentType = headerValue(input.headers, "content-type");
  const signatureInput = headerValue(input.headers, "signature-input") ?? "";
  const signatureHeader = headerValue(input.headers, "signature") ?? "";
  const inputMatch = RESPONSE_INPUT.exec(signatureInput);
  const signatureMatch = /^sig1=:([A-Za-z0-9+/]{86}==):$/.exec(signatureHeader);
  if (inputMatch === null || signatureMatch === null) {
    throw new UcpSignatureError("signature_invalid");
  }
  const expectedComponents = input.body.length > 0 ? RESPONSE_WITH_BODY : RESPONSE_WITHOUT_BODY;
  const created = Number(inputMatch[2]);
  const keyId = inputMatch[3];
  const expires = Number(inputMatch[4]);
  if (
    inputMatch[1] !== expectedComponents ||
    keyId !== input.expectedKeyId ||
    !validTimes(created, expires) ||
    created > input.now + 5 ||
    expires < input.now - 5 ||
    !Number.isInteger(input.status) ||
    input.status < 100 ||
    input.status > 599 ||
    input.publicKey.type !== "public" ||
    input.publicKey.asymmetricKeyType !== "ec" ||
    input.publicKey.asymmetricKeyDetails?.namedCurve !== "prime256v1"
  ) {
    throw new UcpSignatureError("signature_invalid");
  }
  if (input.body.length > 0) {
    verifyContentDigest(input.body, digest);
    if (!contentType || /[\r\n]/.test(contentType)) {
      throw new UcpSignatureError("components_invalid");
    }
  }
  const components = input.body.length > 0
    ? [
        `"@status": ${input.status}`,
        `"content-digest": ${digest}`,
        `"content-type": ${contentType}`,
      ]
    : [`"@status": ${input.status}`];
  const base = [...components, `"@signature-params": ${signatureInput.slice(5)}`].join("\n");
  const signature = Buffer.from(signatureMatch[1], "base64");
  if (
    signature.length !== 64 ||
    signature.toString("base64") !== signatureMatch[1] ||
    !verifyBytes(
      "sha256",
      Buffer.from(base),
      { key: input.publicKey, dsaEncoding: "ieee-p1363" },
      signature,
    )
  ) {
    throw new UcpSignatureError("signature_invalid");
  }
};
