import { useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useSearch, flattenSearchResults } from '@/hooks/useSearch'
import { FeedCard } from '@/components/FeedCard'

export function SearchPage() {
  const [searchParams] = useSearchParams()
  const query = searchParams.get('q') ?? ''
  const {
    data,
    error,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading,
    refetch,
  } = useSearch(query)
  const sentinelRef = useRef<HTMLDivElement>(null)

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

  if (!query.trim()) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <p className="text-gray-400">输入关键词搜索</p>
      </div>
    )
  }

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

  const feeds = flattenSearchResults(data?.pages)

  if (feeds.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <p className="text-gray-400">未找到相关帖子</p>
        <p className="mt-2 text-xs text-gray-400">
          关键词：<span className="text-gray-600">{query}</span>
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-500">
        关键词 <span className="text-gray-700">「{query}」</span> 的搜索结果
      </p>
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
