import Head from 'next/head'
import { useState, useEffect } from 'react'
import { apiClient } from '@/services/api'
import { Brain } from 'lucide-react'
import SentimentDashboard from '@/components/SentimentDashboard'

export default function SentimentPage() {
  const [aiStatus, setAiStatus] = useState<any>(null)
  const [aiAgents, setAiAgents] = useState<any[]>([])

  useEffect(() => {
    apiClient.getAgentStatus().then(res => setAiStatus(res.data)).catch(() => {})
    apiClient.getAgents().then(res => setAiAgents(res.data?.agents || [])).catch(() => {})
  }, [])

  const sentimentAgent = aiAgents.find(a => a.role === 'sentiment_analyst')
  const marketAgent = aiAgents.find(a => a.role === 'market_analyst')

  return (
    <>
      <Head><title>TradeBot - Sentiment</title></Head>

      <div className="space-y-6 max-w-7xl mx-auto">
        <div>
          <h1 className="text-3xl font-bold text-white">Sentiment Analysis</h1>
          <p className="text-sm text-gray-400 mt-1">
            Market sentiment from news, social media, and on-chain data
          </p>
        </div>

        {/* AI Agents for Sentiment */}
        {aiStatus && (
          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                <Brain className="w-4 h-4 text-purple-400" /> AI Sentiment Agents
              </h3>
              <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                aiStatus.ai_enabled ? 'bg-green-500/20 text-green-400 border border-green-500/30' : 'bg-red-500/20 text-red-400 border border-red-500/30'
              }`}>
                {aiStatus.ai_enabled ? 'AI Active' : 'AI Disabled'}
              </span>
            </div>
            {aiStatus.ai_enabled && (
              <div className="flex flex-wrap gap-2">
                {[sentimentAgent, marketAgent].filter(Boolean).map((agent: any) => (
                  <div key={agent.id} className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs ${
                    agent.is_active
                      ? 'bg-purple-500/15 border border-purple-500/30 text-purple-300'
                      : 'bg-gray-800 border border-gray-700 text-gray-500'
                  }`}>
                    <span className={`w-1.5 h-1.5 rounded-full ${agent.is_active ? 'bg-green-400' : 'bg-gray-600'}`} />
                    <span className="font-medium">{agent.name}</span>
                    <span className="text-gray-500">{agent.model}</span>
                    {agent.is_active && <span className="text-green-400">Active</span>}
                  </div>
                ))}
                {!sentimentAgent && !marketAgent && (
                  <span className="text-xs text-gray-500">No sentiment agents configured. Go to Agents page to set up.</span>
                )}
              </div>
            )}
            {!aiStatus.ai_enabled && (
              <p className="text-xs text-gray-500">
                Enable AI agents on the Agents page to get AI-powered sentiment analysis alongside RSS feeds.
              </p>
            )}
          </div>
        )}

        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <SentimentDashboard />
        </div>
      </div>
    </>
  )
}
