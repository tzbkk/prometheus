import { useInfiniteQuery } from '@tanstack/react-query'
import { DEFAULT_PAGE_SIZE, searchFeeds, type Feed } from '@/lib/api'

/**
 * Infinite paginated search. The query key embeds the trimmed query and
 * optional `guildId` so a new search or guild switch resets the cache;
 * disabled when the query is empty to avoid firing an empty-q request.
 */
export function useSearch(
  query: string,
  size = DEFAULT_PAGE_SIZE,
  guildId?: string | null,
) {
  const trimmed = query.trim()
  return useInfiniteQuery({
    queryKey: ['search', trimmed, guildId ?? null],
    queryFn: ({ pageParam }) => searchFeeds(trimmed, pageParam, size, guildId),
    initialPageParam: 1,
    enabled: trimmed.length > 0,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === size ? allPages.length + 1 : undefined,
  })
}

/** Flattened search-result list across all loaded pages. */
export function flattenSearchResults(pages: Feed[][] | undefined): Feed[] {
  return pages?.flat() ?? []
}
