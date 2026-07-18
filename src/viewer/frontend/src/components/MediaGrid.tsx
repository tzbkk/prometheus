import { useState } from 'react'
import { mediaUrl, type Media } from '@/lib/api'
import { Lightbox } from '@/components/Lightbox'

interface MediaGridProps {
  media: Media[]
  guildId: string
}

export function MediaGrid({ media, guildId }: MediaGridProps) {
  const [lightbox, setLightbox] = useState<string | null>(null)

  if (media.length === 0) return null

  return (
    <>
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {media.map((item, idx) => {
          if (!item.file) return null
          const src = mediaUrl(guildId, item.file)
          if (!src) return null
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

      <Lightbox src={lightbox} onClose={() => setLightbox(null)} />
    </>
  )
}
