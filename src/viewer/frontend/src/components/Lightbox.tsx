interface LightboxProps {
  src: string | null
  onClose: () => void
}

export function Lightbox({ src, onClose }: LightboxProps) {
  if (!src) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4"
      onClick={onClose}
    >
      <img
        src={src}
        alt="fullsize"
        className="max-h-full max-w-full object-contain"
      />
      <button
        className="absolute right-4 top-4 rounded-full bg-white/20 px-3 py-1 text-sm text-white hover:bg-white/30"
        onClick={(e) => {
          e.stopPropagation()
          onClose()
        }}
      >
        ✕
      </button>
    </div>
  )
}
