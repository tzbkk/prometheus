import { Route, Routes, useParams } from 'react-router-dom'
import { Layout } from '@/components/Layout'
import { FeedListPage } from '@/pages/FeedListPage'
import { FeedDetailPage } from '@/pages/FeedDetailPage'
import { SearchPage } from '@/pages/SearchPage'

function FeedDetailRoute() {
  const { id = '' } = useParams()
  return <FeedDetailPage id={id} />
}

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<FeedListPage />} />
        <Route path="/feed/:id" element={<FeedDetailRoute />} />
        <Route path="/search" element={<SearchPage />} />
      </Routes>
    </Layout>
  )
}
