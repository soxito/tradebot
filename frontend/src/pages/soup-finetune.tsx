import dynamic from 'next/dynamic';
const SoupFinetunePage = dynamic(() => import('../components/SoupFinetunePageContent'), {
  ssr: false,
  loading: () => <div className="p-4 text-gray-400 text-sm">Loading Soup Lab…</div>,
});
export default SoupFinetunePage;
