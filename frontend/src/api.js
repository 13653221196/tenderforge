export async function api(path, { method = 'GET', body, form } = {}) {
  const opt = { method, credentials: 'same-origin', headers: {} }
  if (form) {
    opt.body = form
  } else if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json'
    opt.body = JSON.stringify(body)
  }
  const r = await fetch(path, opt)
  let data = null
  try {
    data = await r.json()
  } catch {
    data = null
  }
  if (r.status === 401) {
    location.replace('/login.html')
    throw new Error('登录已过期，请重新登录')
  }
  if (!r.ok) {
    const msg = typeof data?.detail === 'string'
      ? data.detail
      : data?.error || data?.detail?.msg || `请求失败 ${r.status}`
    const err = new Error(msg)
    err.status = r.status
    err.body = data
    throw err
  }
  return data
}
