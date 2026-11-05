import { toast } from "sonner";
import { successMessage, worthAToast } from "./toasts";
/** Thin fetch wrapper. The session is an httpOnly cookie; the CSRF token lives only in memory. */

let csrf: string | null = null;

export function setCsrf(token: string | null): void {
  csrf = token;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
  }
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

type Query = Record<string, string | number | boolean | undefined | null>;

async function request<T>(
  path: string,
  opts: {
    method?: string;
    body?: unknown;
    query?: Query;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const method = opts.method ?? "GET";
  const qs = opts.query
    ? Object.entries(opts.query)
        .filter(([, v]) => v !== undefined && v !== null && v !== "")
        .map(
          ([k, v]) =>
            `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`,
        )
        .join("&")
    : "";
  const headers: Record<string, string> = {};
  const isForm =
    typeof FormData !== "undefined" && opts.body instanceof FormData;
  if (opts.body !== undefined && !isForm)
    headers["Content-Type"] = "application/json";
  if (UNSAFE.has(method) && csrf) headers["X-CSRF-Token"] = csrf;

  const res = await fetch(`/api${path}${qs ? `?${qs}` : ""}`, {
    method,
    headers,
    credentials: "same-origin",
    body:
      opts.body === undefined
        ? undefined
        : isForm
          ? (opts.body as FormData)
          : JSON.stringify(opts.body),
    signal: opts.signal,
  });

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = undefined;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const d = (data as { detail?: unknown } | undefined)?.detail;
    const msg =
      typeof d === "string"
        ? d
        : res.status === 401
          ? "Please sign in again."
          : `Request failed (${res.status})`;
    throw new ApiError(res.status, msg, d);
  }
  return data as T;
}

/** Every call goes through here so a change can confirm itself and a real failure can say so. */
export async function api<T>(
  path: string,
  opts: Parameters<typeof request>[1] = {},
): Promise<T> {
  const method = opts.method ?? "GET";
  try {
    const out = await request<T>(path, opts);
    const ok = successMessage(method, path);
    if (ok) toast.success(ok);
    return out;
  } catch (e) {
    const status = e instanceof ApiError ? e.status : null;
    const message = e instanceof Error ? e.message : "";
    if (
      UNSAFE.has(method) &&
      !(e instanceof DOMException) &&
      worthAToast(status, message)
    ) {
      toast.error(
        e instanceof ApiError
          ? message
          : "Could not reach the server. Check your connection and try again.",
      );
    }
    throw e;
  }
}
