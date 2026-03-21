// HTTP request helper — extracted from server.py L851-854
export async function api<T = Record<string, unknown>>(
  method: 'GET' | 'POST',
  url: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
  }
  if (body) opts.body = JSON.stringify(body)
  const res = await fetch(url, opts)
  return res.json() as Promise<T>
}
