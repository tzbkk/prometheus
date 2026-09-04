import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useFeeds, flattenFeeds } from '@/hooks/useFeeds'
import { rebuild } from '@/lib/api'
import { useGuild } from '@/lib/guild-context'
import { FeedCard } from '@/components/FeedCard'

export function FeedListPage() {
  const queryClient = useQueryClient()
  const { selectedGuild } = useGuild()
  const {
    data,
    error,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading,
    refetch,
  } = useFeeds(undefined, selectedGuild)
  const sentinelRef = useRef<HTMLDivElement>(null)
  const scrollYRef = useRef(0)

  const handleRebuild = async () => {
    await rebuild()
    await queryClient.invalidateQueries({ queryKey: ['feeds'] })
  }

  useEffect(() => {
    const handler = () => { scrollYRef.current = window.scrollY }
    window.addEventListener('scroll', handler, { passive: true })
    return () => {
      window.removeEventListener('scroll', handler)
      sessionStorage.setItem('feedListScrollY', String(scrollYRef.current))
    }
  }, [])

  useEffect(() => {
    const saved = sessionStorage.getItem('feedListScrollY')
    if (saved) {
      sessionStorage.removeItem('feedListScrollY')
      requestAnimationFrame(() =>
        requestAnimationFrame(() =>
          window.scrollTo(0, Number(saved)),
        ),
      )
    }
  }, [])

  useEffect(() => {
    const sentinel = sentinelRef.current
    if (!sentinel) return

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && hasNextPage && !isFetchingNextPage) {
          fetchNextPage()
        }
      },
      { rootMargin: '200px' },
    )
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [fetchNextPage, hasNextPage, isFetchingNextPage])

  const feeds = flattenFeeds(data?.pages)

  if (isLoading) {
    return <SkeletonList />
  }

  if (error) {
    return (
      <div className="rounded-lg border border-gray-200 bg-gray-50 p-8 text-center">
        <p className="text-sm text-gray-500">加载失败</p>
        <p className="mt-1 break-all text-xs text-gray-400">{error.message}</p>
        <button
          onClick={() => refetch()}
          className="mt-4 rounded border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100"
        >
          重试
        </button>
      </div>
    )
  }

  if (feeds.length === 0) {
    return <EmptyState onRebuild={handleRebuild} />
  }

  return (
    <div className="space-y-4">
      {feeds.map((feed) => (
        <FeedCard key={feed.id} feed={feed} />
      ))}
      <div ref={sentinelRef} className="h-1" />
      {isFetchingNextPage && <SkeletonList count={3} />}
      {!hasNextPage && feeds.length > 0 && (
        <p className="py-4 text-center text-sm text-gray-400">没有更多了</p>
      )}
    </div>
  )
}

function SkeletonList({ count = 5 }: { count?: number }) {
  return (
    <div className="space-y-4">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="animate-pulse rounded-lg bg-white p-4 shadow-md">
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 shrink-0 rounded-full bg-gray-200" />
            <div className="flex-1 space-y-2">
              <div className="h-3 w-24 rounded bg-gray-200" />
              <div className="h-2 w-32 rounded bg-gray-200" />
            </div>
          </div>
          <div className="mt-3 space-y-2">
            <div className="h-3 w-full rounded bg-gray-200" />
            <div className="h-3 w-3/4 rounded bg-gray-200" />
          </div>
          <div className="mt-3 h-48 w-full rounded bg-gray-200" />
        </div>
      ))}
    </div>
  )
}

function EmptyState({ onRebuild }: { onRebuild: () => Promise<void> }) {
  const [rebuilding, setRebuilding] = useState(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  async function handleClick() {
    setRebuilding(true)
    setErrorMsg(null)
    try {
      await onRebuild()
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : String(err))
    } finally {
      setRebuilding(false)
    }
  }

  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <p className="text-gray-400">暂无帖子</p>
      <button
        onClick={handleClick}
        disabled={rebuilding}
        className="mt-4 rounded border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 disabled:opacity-50"
      >
        {rebuilding ? '重建中...' : '点击重建数据库'}
      </button>
      {errorMsg && (
        <p className="mt-2 break-all text-xs text-red-400">{errorMsg}</p>
      )}
    </div>
  )
}
