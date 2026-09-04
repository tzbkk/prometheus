import { useState } from 'react'
import { type Media } from '@/lib/api'
import { Lightbox } from '@/components/Lightbox'

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
          const path = item.path
          if (!path) return null
          const isVideo = item.type === 'video'

          return isVideo ? (
            <video
              key={`${path}-${idx}`}
              controls
              src={path}
              className="w-full rounded bg-black"
            >
              您的浏览器不支持视频播放。
            </video>
          ) : (
            <img
              key={`${path}-${idx}`}
              loading="lazy"
              src={path}
              alt={item.url ?? `media-${idx + 1}`}
              onClick={() => setLightbox(path)}
              className="h-64 w-full cursor-zoom-in rounded bg-gray-100 object-cover"
            />
          )
        })}
      </div>

      <Lightbox src={lightbox} onClose={() => setLightbox(null)} />
    </>
  )
}
