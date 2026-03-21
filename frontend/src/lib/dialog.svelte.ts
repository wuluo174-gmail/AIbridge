let open = $state(false)
let message = $state('')
let mode = $state<'confirm' | 'alert'>('confirm')
let resolver: ((v: boolean) => void) | null = null

function dismiss() {
  if (!resolver) return
  const prev = resolver
  resolver = null
  open = false
  prev(false)
}

export function showConfirm(msg: string): Promise<boolean> {
  dismiss()
  return new Promise(resolve => {
    message = msg
    mode = 'confirm'
    resolver = resolve
    open = true
  })
}

export function showAlert(msg: string): Promise<void> {
  dismiss()
  return new Promise<void>(resolve => {
    message = msg
    mode = 'alert'
    resolver = () => resolve()
    open = true
  })
}

function resolve(value: boolean) {
  open = false
  resolver?.(value)
  resolver = null
}

export const dialog = {
  get open() { return open },
  get message() { return message },
  get mode() { return mode },
  resolve,
}
