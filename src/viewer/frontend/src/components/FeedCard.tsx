import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { Feed } from '@/lib/api'

interface FeedCardProps {
  feed: Feed
}

function formatTime(unixSeconds: number | null): string {
  if (!unixSeconds) return ''
  return new Date(unixSeconds * 1000).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function FeedCard({ feed }: FeedCardProps) {
  const [avatarError, setAvatarError] = useState(false)
  const firstLetter = feed.author_nick?.charAt(0) ?? '?'
  const hasImage = feed.image_count > 0 && Boolean(feed.first_media)

  return (
    <Link
      to={`/feed/${feed.id}`}
      className="block rounded-lg bg-white p-4 shadow-md transition hover:shadow-xl"
    >
      <div className="flex items-center gap-3">
        {avatarError || !feed.author_avatar ? (
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-gray-200 text-sm font-medium text-gray-600">
            {firstLetter}
          </div>
        ) : (
          <img
            src={feed.author_avatar}
            alt={feed.author_nick ?? 'avatar'}
            className="h-10 w-10 shrink-0 rounded-full object-cover"
            onError={() => setAvatarError(true)}
          />
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-gray-900">
            {feed.author_nick ?? 'Unknown'}
          </p>
          <p className="text-xs text-gray-500">{formatTime(feed.create_time)}</p>
        </div>
      </div>

      {feed.title_text && (
        <p className="mt-3 line-clamp-3 text-sm text-gray-700">
          {feed.title_text}
        </p>
      )}

      {hasImage && (
        <img
          loading="lazy"
          src={`/media/${feed.first_media}`}
          alt={feed.title_text ?? ''}
          className="mt-3 h-48 w-full rounded object-cover"
        />
      )}
    </Link>
  )
}
