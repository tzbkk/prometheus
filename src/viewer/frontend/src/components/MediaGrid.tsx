import { useState } from 'react'
import type { Media } from '@/lib/api'

interface MediaGridProps {
  media: Media[]
}

export function MediaGrid({ media }: MediaGridProps) {
  const [lightbox, setLightbox] = useState<string | null>(null)

  if (media.length === 0) return null

  return (
    <>
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {media.map((item, idx) => {
          if (!item.file) return null
          const src = `/media/${item.file}`
          const isVideo = item.type === 'video'

          return isVideo ? (
            <video
              key={`${item.file}-${idx}`}
              controls
              src={src}
              className="w-full rounded bg-black"
            >
              您的浏览器不支持视频播放。
            </video>
          ) : (
            <img
              key={`${item.file}-${idx}`}
              loading="lazy"
              src={src}
              alt={item.file}
              onClick={() => setLightbox(src)}
              className="h-64 w-full cursor-zoom-in rounded bg-gray-100 object-cover"
            />
          )
        })}
      </div>

      {lightbox && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4"
          onClick={() => setLightbox(null)}
        >
          <img
            src={lightbox}
            alt="fullsize"
            className="max-h-full max-w-full object-contain"
          />
          <button
            className="absolute right-4 top-4 rounded-full bg-white/20 px-3 py-1 text-sm text-white hover:bg-white/30"
            onClick={(e) => {
              e.stopPropagation()
              setLightbox(null)
            }}
          >
            ✕
          </button>
        </div>
      )}
    </>
  )
}
