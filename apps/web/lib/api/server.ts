import { cookies } from "next/headers";

const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

/**
 * Server-component fetch helper: forwards the session cookie to the API so
 * initial page data loads with the caller's identity (D-239). There is no
 * session cookie yet — BUILD-02 (identity_auth) issues it — so today this
 * forwards whatever cookie header exists, which is none.
 */
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const cookieStore = await cookies();
  const cookieHeader = cookieStore.toString();

  return fetch(`${API_INTERNAL_URL}${path}`, {
    ...init,
    headers: {
      ...init?.headers,
      ...(cookieHeader ? { Cookie: cookieHeader } : {}),
    },
    cache: "no-store",
  });
}
