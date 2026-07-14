import { useInfiniteQuery } from '@tanstack/react-query'
import { DEFAULT_PAGE_SIZE, fetchFeeds, type Feed } from '@/lib/api'

/**
 * Infinite paginated feed list (newest first). The query key is the stable
 * `['feeds']`; pages are appended via getNextPageParam when the last page
 * returned a full page_size batch.
 */
export function useFeeds(size = DEFAULT_PAGE_SIZE) {
  return useInfiniteQuery({
    queryKey: ['feeds'],
    queryFn: ({ pageParam }) => fetchFeeds(pageParam, size),
    initialPageParam: 1,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === size ? allPages.length + 1 : undefined,
    refetchInterval: 30_000,
  })
}

/** Flattened feed list across all loaded pages; convenient for list rendering. */
export function flattenFeeds(pages: Feed[][] | undefined): Feed[] {
  return pages?.flat() ?? []
}
