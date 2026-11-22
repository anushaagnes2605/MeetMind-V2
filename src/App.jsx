
import { useState, useEffect, useRef, useCallback } from 'react'
import './App.css'

function authFetch(url, options = {}) {
  return fetch(url, {
    ...options,
    credentials: 'include',
  }).then((response) => {
    if (response.status === 401) {
      window.dispatchEvent(new Event('meetmind:unauthorized'))
    }
    return response
  })
}

function getApiUrl() {
  const configured = (import.meta.env.VITE_API_URL || '').trim()
  if (configured) return configured.replace(/\/$/, '')
  if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
    return 'http://localhost:8001'
  }
  return ''
}

const API_NOT_CONFIGURED =
  'This deployment cannot reach the MeetMind API. Set VITE_API_URL to the deployed backend URL and redeploy the frontend.'

const navigation = [
  {
    heading: 'WORKSPACE',
    items: [
      { id: 'overview', label: 'Overview', icon: '◫' },
      { id: 'meetings', label: 'Meetings', icon: '▤' },
      { id: 'decisions', label: 'Decisions', icon: '◇' },
      { id: 'actions', label: 'Action center', icon: '✓' },
    ],
  },
  {
    heading: 'INTELLIGENCE',
    items: [
      { id: 'drift', label: 'Decision drift', icon: '⌁' },
      { id: 'memory', label: 'Team memory', icon: '▧' },
      { id: 'activity', label: 'Agent activity', icon: '◎' },
      { id: 'approvals', label: 'Approval center', icon: '!' },
    ],
  },
]

const pageDetails = {
  meetings: {
    eyebrow: 'WORKSPACE',
    title: 'Meetings',
    description: 'Your conversations, transcripts and meeting outcomes.',
    message: 'Your meeting library will appear here. Soon, you will be able to upload a transcript or paste meeting notes for AI processing.',
  },
  decisions: {
    eyebrow: 'WORKSPACE',
    title: 'Decisions',
    description: 'A searchable record of what your team has agreed on.',
    message: 'Decisions extracted from your meetings will be collected here, along with their owners and source meetings.',
  },
  actions: {
    eyebrow: 'WORKSPACE',
    title: 'Action center',
    description: 'Track commitments and follow-ups across your team.',
    message: 'Your action items will appear here with owners, deadlines, priorities and completion status.',
  },
  drift: {
    eyebrow: 'INTELLIGENCE',
    title: 'Decision drift',
    description: 'Find contradictions between decisions made at different times.',
    message: 'MeetMind will compare new meeting decisions with earlier ones and highlight possible conflicts for your review.',
  },
  memory: {
    eyebrow: 'INTELLIGENCE',
    title: 'Team memory',
    description: 'The context your team should not have to repeat.',
    message: 'Important decisions, project context and previous commitments will be stored here for future meetings.',
  },
  activity: {
    eyebrow: 'INTELLIGENCE',
    title: 'Agent activity',
    description: 'Review persisted orchestrator runs and execution steps.',
    message: 'Agent workflow activity will appear here.',
  },
  approvals: {
    eyebrow: 'INTELLIGENCE',
    title: 'Approval center',
    description: 'Review consequential agent proposals before local execution.',
    message: 'Approval requests will appear here.',
  },
}

function ApprovalCenter({ requests, onChanged }) {
  const [busyId, setBusyId] = useState(null)
  const [message, setMessage] = useState('')

  async function submitDecision(approvalId, decision) {
    setBusyId(approvalId)
    setMessage('')
    try {
      const backendUrl = getApiUrl()
      const response = await authFetch(`${backendUrl}/approvals/${approvalId}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision }),
      })
      if (!response.ok) throw new Error('Could not update approval status.')
      await response.json()
      setMessage(`Approval request ${approvalId} was ${decision}.`)
      onChanged()
    } catch (error) {
      setMessage(error.message || 'Approval update failed.')
    } finally {
      setBusyId(null)
    }
  }

  async function executeRequest(approvalId) {
    setBusyId(approvalId)
    setMessage('')
    try {
      const backendUrl = getApiUrl()
      const response = await authFetch(`${backendUrl}/approvals/${approvalId}/execute`, {
        method: 'POST',
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || 'Execution failed.')
      setMessage(
        result.execution_status === 'executed'
          ? `Approval ${approvalId} executed successfully.`
          : `Approval ${approvalId} execution failed.`
      )
      onChanged()
    } catch (error) {
      setMessage(error.message || 'Execution failed.')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="panel approval-panel">
      <div className="list-panel-header">
        <div>
          <h2>Human approval queue</h2>
          <p>Proposed local actions require explicit approval</p>
        </div>
        <span className="list-count">
          {requests.filter((request) => request.status === 'pending').length} pending
        </span>
      </div>

      {message && <div className="form-message" role="status">{message}</div>}

      {requests.length === 0 ? (
        <div className="empty-state">
          <h2>No approval requests</h2>
          <p>Agent proposals will appear here before local execution.</p>
        </div>
      ) : requests.map((request) => (
        <article className="approval-card" key={request.id}>
          <div className="approval-card-header">
            <div>
              <span className="agent-run-id">Approval #{request.id}</span>
              <h3>{request.tool_name.replaceAll('_', ' ')}</h3>
            </div>
            <span className={`approval-status approval-${request.status}`}>
              {request.status}
            </span>
          </div>

          <div className="approval-meta">
            <span>Run #{request.run_id}</span>
            {request.meeting_id && <span>Meeting #{request.meeting_id}</span>}
          </div>

          <p className="approval-reason">{request.reason}</p>

          {request.evidence && (
            <div className="approval-evidence">
              <strong>Supporting evidence</strong>
              <p>
                {request.evidence.task || 'Local action proposal'}
                {request.evidence.urgency && ` · ${request.evidence.urgency} urgency`}
              </p>
            </div>
          )}

          {request.status === 'approved' && (
            <div className={`execution-status execution-${request.execution_status}`}>
              <strong>Execution:</strong> {request.execution_status.replaceAll('_', ' ')}
              {request.execution_result?.message && <p>{request.execution_result.message}</p>}
            </div>
          )}
          {request.status === 'rejected' && (
            <div className="execution-status execution-not-executed">
              <strong>Execution:</strong> not executed
            </div>
          )}

          {request.status === 'pending' && (
            <div className="approval-actions">
              <button
                type="button"
                className="secondary-button"
                disabled={busyId === request.id}
                onClick={() => submitDecision(request.id, 'rejected')}
              >
                Reject
              </button>
              <button
                type="button"
                className="primary-button"
                disabled={busyId === request.id}
                onClick={() => submitDecision(request.id, 'approved')}
              >
                Approve
              </button>
            </div>
          )}

          {request.status === 'approved' && request.execution_status === 'not_executed' && (
            <div className="approval-actions">
              <button
                type="button"
                className="primary-button"
                disabled={busyId === request.id}
                onClick={() => executeRequest(request.id)}
              >
                Execute approved action
              </button>
            </div>
          )}
        </article>
      ))}
    </section>
  )
}

function AgentActivity({ runs, toolExecutions, approvals }) {
  const [runDetails, setRunDetails] = useState({})

  useEffect(() => {
    const backendUrl = getApiUrl()
    let cancelled = false

    Promise.all(
      runs.map((run) =>
        authFetch(`${backendUrl}/agent/runs/${run.id}`)
          .then((response) => response.json())
          .then((detail) => [run.id, detail])
          .catch(() => [run.id, null])
      )
    ).then((entries) => {
      if (!cancelled) {
        setRunDetails(Object.fromEntries(entries.filter(([, detail]) => detail)))
      }
    })

    return () => {
      cancelled = true
    }
  }, [runs])

  function parseToolResult(execution) {
    if (!execution.result_summary) return null
    try {
      return JSON.parse(execution.result_summary)
    } catch {
      return null
    }
  }

  return (
    <section className="panel activity-panel">
      <div className="list-panel-header">
        <div>
          <h2>Execution timeline</h2>
          <p>Persisted orchestrator runs and planned steps</p>
        </div>
        <span className="list-count">{runs.length} runs</span>
      </div>

      {runs.length === 0 ? (
        <div className="empty-state">
          <h2>No agent activity yet</h2>
          <p>Submit a meeting to initialize its orchestrator run.</p>
        </div>
      ) : runs.map((run) => {
        const detail = runDetails[run.id]
        const steps = detail?.steps || []
        return (
          <article className="agent-run" key={run.id}>
            <div className="agent-run-header">
              <div>
                <span className="agent-run-id">Run #{run.id}</span>
                <h3>{run.meeting_title || 'Workspace run'}</h3>
              </div>
              <div className="agent-run-badges">
                <span className="provider-badge">{run.provider}</span>
                <span className={`run-status status-${run.status}`}>{run.status}</span>
              </div>
            </div>

            <div className="agent-step-list">
              {steps.map((step) => (
                <div className="agent-step" key={step.id}>
                  <div className="agent-step-marker" />
                  <div>
                    <div className="agent-step-heading">
                      <strong>{step.agent_role}</strong>
                      <span>{step.operation}</span>
                    </div>
                    <p>{step.description}</p>
                  </div>
                  <span className={`step-status status-${step.status}`}>{step.status}</span>
                </div>
              ))}
            </div>
          </article>
        )
      })}

      <div className="approval-timeline">
        <div className="tool-history-header">
          <h3>Approval sequence</h3>
          <span>{approvals.length} requests</span>
        </div>
        {approvals.length === 0 ? (
          <p className="muted-copy">No approval requests recorded.</p>
        ) : approvals.map((approval) => (
          <div className="approval-timeline-row" key={approval.id}>
            <div>
              <strong>Approval #{approval.id}</strong>
              <p>Run #{approval.run_id} · {approval.tool_name.replaceAll('_', ' ')}</p>
            </div>
            <div className="approval-timeline-stages">
              <span>Proposed</span>
              <span className={`approval-status approval-${approval.status}`}>
                {approval.status}
              </span>
              {approval.status !== 'pending' && (
                <span className={`execution-status execution-${approval.execution_status}`}>
                  {approval.execution_status.replaceAll('_', ' ')}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="tool-history">
        <div className="tool-history-header">
          <h3>Tool execution history</h3>
          <span>{toolExecutions.length} executions</span>
        </div>
        {toolExecutions.length === 0 ? (
          <p className="muted-copy">No tools executed in this phase.</p>
        ) : toolExecutions.map((execution) => {
          const result = parseToolResult(execution)
          return (
            <div className="tool-execution" key={execution.id}>
              <strong>{execution.tool_name}</strong>
              <span>{execution.status}</span>
              {execution.tool_name === 'search_memory' && result ? (
                <div className="tool-result-details">
                  <p>
                    {result.decision_count ?? 0} previous decisions · {result.open_action_count ?? 0} open actions
                  </p>
                  {result.meeting_ids?.length > 0 && (
                    <p>Meetings: {result.meeting_ids.join(', ')}</p>
                  )}
                  {result.decisions?.slice(0, 5).map((decision) => (
                    <blockquote key={decision.decision_id}>
                      Meeting #{decision.meeting_id}: {decision.decision}
                      <span>Source: “{decision.source_passage}”</span>
                    </blockquote>
                  ))}
                </div>
              ) : execution.tool_name === 'compare_decisions' && result ? (
                <div className="tool-result-details">
                  <p className={`comparison-badge classification-${result.classification}`}>
                    {result.classification.replace('_', ' ')} · {Math.round(result.confidence * 100)}% · {result.provider}
                  </p>
                  <p>{result.explanation}</p>
                  {result.evidence?.slice(0, 5).map((item) => (
                    <blockquote key={item.decision_id}>
                      {item.meeting_title || `Meeting #${item.meeting_id}`}:
                      <span>Source: “{item.source_passage}”</span>
                    </blockquote>
                  ))}
                </div>
              ) : execution.tool_name === 'analyze_commitments' && result ? (
                <div className="tool-result-details">
                  <p>
                    {result.open_count} open · {result.overdue_count} overdue · {result.approaching_count} approaching · {result.unassigned_count} unassigned · {result.completed_count} completed · {result.duplicate_count} duplicates
                  </p>
                  {result.commitments?.slice(0, 8).map((commitment) => (
                    <blockquote key={commitment.action_id}>
                      <strong>{commitment.task}</strong>
                      <span>
                        Owner: {commitment.owner || 'Unassigned'} · Deadline: {commitment.deadline || 'Missing'} · Status: {commitment.status}
                      </span>
                      <span>Meeting #{commitment.meeting_id} · Source: “{commitment.source_passage}”</span>
                    </blockquote>
                  ))}
                </div>
              ) : execution.tool_name === 'calculate_commitment_risk' && result ? (
                <div className="tool-result-details">
                  {result.ranked_commitments?.slice(0, 8).map((commitment) => (
                    <blockquote key={commitment.action_id}>
                      <strong>{commitment.task}</strong>
                      <span className={`risk-badge risk-${commitment.risk_level}`}>
                        {commitment.risk_level} · {commitment.risk_score}/100
                      </span>
                      {commitment.factors?.map((factor) => (
                        <span key={factor}>{factor}</span>
                      ))}
                    </blockquote>
                  ))}
                </div>
              ) : execution.tool_name === 'recommend_interventions' && result ? (
                <div className="tool-result-details">
                  {result.map((recommendation) => (
                    <blockquote key={`${recommendation.action_id}-${recommendation.intervention}`}>
                      <strong>{recommendation.task}</strong>
                      <span>
                        {recommendation.intervention.replaceAll('_', ' ')} · {recommendation.urgency}
                      </span>
                      <span>{recommendation.reason}</span>
                    </blockquote>
                  ))}
                  {result.length === 0 && <p>No interventions recommended.</p>}
                </div>
              ) : execution.tool_name === 'coordinated_workflow' && result ? (
                <div className="tool-result-details">
                  <p>
                    Memory: {result.memory_status} · Decisions: {result.comparison_status} ·
                    Commitments: {result.commitment_status} · Risk: {result.risk_status} ·
                    Interventions: {result.intervention_status}
                  </p>
                  <p>
                    {result.pending_approvals} pending approval{result.pending_approvals === 1 ? '' : 's'} ·
                    {result.comparisons} decision comparison{result.comparisons === 1 ? '' : 's'}
                  </p>
                </div>
              ) : (
                <p>{execution.result_summary || 'No result summary recorded.'}</p>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

function MeetingInput() {
  const [title, setTitle] = useState('')
  const [transcript, setTranscript] = useState('')
  const [textFileName, setTextFileName] = useState('')
  const [audioFile, setAudioFile] = useState(null)
  const [message, setMessage] = useState('')
  const [analysis, setAnalysis] = useState(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  async function handleTextUpload(event) {
    const file = event.target.files[0]
    if (!file) return

    const allowedExtensions = ['txt', 'md', 'vtt']
    const extension = file.name.split('.').pop().toLowerCase()

    if (!allowedExtensions.includes(extension)) {
      setMessage('Please upload a TXT, MD or VTT transcript.')
      return
    }

    const content = await file.text()
    setTranscript(content)
    setTextFileName(file.name)
    setMessage('Transcript loaded successfully.')
  }

  function handleAudioUpload(event) {
    const file = event.target.files[0]
    if (!file) return

    const extension = file.name.split('.').pop().toLowerCase()
    const allowedExtensions = ['mp3', 'wav', 'm4a', 'aac', 'ogg', 'webm']

    if (!file.type.startsWith('audio/') &&
        !allowedExtensions.includes(extension)) {
      setMessage('Please upload a supported audio recording.')
      return
    }

    if (file.size > 50 * 1024 * 1024) {
      setMessage('Audio file must be smaller than 50 MB for this prototype.')
      return
    }

    setAudioFile(file)
    setMessage('Audio recording loaded successfully.')
  }

  async function handleSubmit(event) {
    event.preventDefault()

    if (!title.trim()) {
      setMessage('Please enter a meeting title.')
      return
    }

    if (!transcript.trim() && !audioFile) {
      setMessage('Please add a transcript or an audio recording.')
      return
    }

    setIsSubmitting(true)
    setMessage('')
    setAnalysis(null)

    try {
      const backendUrl = getApiUrl()
      const response = await authFetch(`${backendUrl}/meetings/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title.trim(), transcript: transcript.trim() }),
      })
      if (!response.ok) {
        const error = await response.json().catch(() => ({}))
        throw new Error(error.detail || 'Meeting analysis failed.')
      }
      const result = await response.json()
      setAnalysis(result)
      const providerLabel =
        result.analysis_mode === 'groq'
          ? 'Groq'
          : result.analysis_mode === 'gemini'
            ? 'Gemini'
            : 'rule-based fallback'
      setMessage(`Meeting processed and saved. Analysis mode: ${providerLabel}.`)
      window.dispatchEvent(new Event('meetmind:data-refresh'))
    } catch (error) {
      setMessage(error.message || 'Could not connect to MeetMind backend.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div className="page-content">
      <section className="subpage-heading">
        <p className="eyebrow">WORKSPACE / MEETINGS</p>
        <h1>New meeting</h1>
        <p className="welcome-subtitle">
          Bring a conversation into MeetMind and turn it into action.
        </p>
      </section>

      <form className="meeting-form" onSubmit={handleSubmit}>
        <section className="panel meeting-form-section">
          <h2>Meeting details</h2>
          <p className="form-description">
            Give this conversation a name so you can find it later.
          </p>

          <label className="form-label" htmlFor="meeting-title">
            Meeting title
          </label>
          <input
            id="meeting-title"
            className="form-input"
            type="text"
            placeholder="e.g. Product Launch Planning"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </section>

        <section className="panel meeting-form-section">
          <h2>Choose your meeting input</h2>
          <p className="form-description">
            Upload a transcript, paste your notes, or add an audio recording.
          </p>

          <div className="input-option">
            <div className="input-option-heading">
              <span className="input-option-icon">▤</span>
              <div>
                <h3>Upload a transcript</h3>
                <p>TXT, MD or VTT files</p>
              </div>
            </div>

            <label className="upload-button">
              Choose transcript file
              <input
                type="file"
                accept=".txt,.md,.vtt,text/plain,text/markdown"
                onChange={handleTextUpload}
                hidden
              />
            </label>

            {textFileName && (
              <p className="file-success">✓ {textFileName}</p>
            )}
          </div>

          <div className="input-divider">
            <span>OR</span>
          </div>

          <div className="input-option">
            <div className="input-option-heading">
              <span className="input-option-icon">✎</span>
              <div>
                <h3>Paste transcript or meeting notes</h3>
                <p>Paste the conversation text below.</p>
              </div>
            </div>

            <textarea
              className="form-textarea"
              placeholder="Paste your meeting transcript here..."
              value={transcript}
              onChange={(event) => {
                setTranscript(event.target.value)
                setTextFileName('')
              }}
              rows={9}
            />
          </div>

          <div className="input-divider">
            <span>AND / OR</span>
          </div>

          <div className="input-option">
            <div className="input-option-heading">
              <span className="input-option-icon">♫</span>
              <div>
                <h3>Upload an audio recording</h3>
                <p>MP3, WAV, M4A, AAC, OGG or WEBM · Maximum 50 MB</p>
              </div>
            </div>

            <label className="upload-button">
              Choose audio file
              <input
                type="file"
                accept="audio/*,.mp3,.wav,.m4a,.aac,.ogg,.webm"
                onChange={handleAudioUpload}
                hidden
              />
            </label>

            {audioFile && (
              <div className="audio-preview">
                <p className="file-success">✓ {audioFile.name}</p>
                <audio
                  controls
                  src={URL.createObjectURL(audioFile)}
                />
                <button
                  type="button"
                  className="remove-file-button"
                  onClick={() => setAudioFile(null)}
                >
                  Remove audio
                </button>
              </div>
            )}
          </div>
        </section>

        {message && (
          <div className="form-message" role="status">
            {message}
          </div>
        )}

        {analysis && (
          <section className="panel analysis-results">
            <div className="panel-header">
              <div>
                <h2>Meeting analysis</h2>
                <p>{analysis.summary}</p>
              </div>
              <span className="processed-badge">{analysis.analysis_mode}</span>
            </div>

            <div className="analysis-grid">
              <div className="analysis-column">
                <h3>Decisions ({analysis.decisions.length})</h3>
                {analysis.decisions.length === 0 ? (
                  <p className="muted-copy">No decisions extracted.</p>
                ) : analysis.decisions.map((decision, index) => (
                  <div className="analysis-item" key={`${decision.text}-${index}`}>
                    <strong>{decision.text}</strong>
                    <p>Source: “{decision.source_passage}”</p>
                  </div>
                ))}
              </div>

              <div className="analysis-column">
                <h3>Action items ({analysis.action_items.length})</h3>
                {analysis.action_items.length === 0 ? (
                  <p className="muted-copy">No action items extracted.</p>
                ) : analysis.action_items.map((action, index) => (
                  <div className="analysis-item" key={`${action.text}-${index}`}>
                    <strong>{action.text}</strong>
                    <p>{action.owner || 'Owner unclear'} · {action.deadline || 'Deadline unclear'} · {action.priority}</p>
                  </div>
                ))}
              </div>
            </div>

            {analysis.fallback_reason && (
              <div className="fallback-callout">
                <strong>Fallback used</strong>
                <p>{analysis.fallback_reason}</p>
              </div>
            )}

            {analysis.important_points?.length > 0 && (
              <div className="important-points">
                <h3>Important discussion points</h3>
                {analysis.important_points.map((point, index) => (
                  <div className="analysis-item" key={`${point.text}-${index}`}>
                    <strong>{point.speaker ? `${point.speaker}: ` : ''}{point.text}</strong>
                    <p>Source: “{point.source_passage}”</p>
                  </div>
                ))}
              </div>
            )}

            {analysis.conflicts.length > 0 && (
              <div className="conflict-callout">
                <strong>{analysis.conflicts.length} possible decision conflict{analysis.conflicts.length === 1 ? '' : 's'} detected</strong>
                {analysis.conflicts.map((conflict, index) => (
                  <p key={index}>{conflict.reason} Compare “{conflict.previous_decision}” from {conflict.previous_meeting} with “{conflict.new_decision}”.</p>
                ))}
              </div>
            )}
          </section>
        )}

        <div className="form-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={() => {
              setTitle('')
              setTranscript('')
              setTextFileName('')
              setAudioFile(null)
              setMessage('')
              setAnalysis(null)
            }}
          >
            Clear form
          </button>

          <button type="submit" className="primary-button" disabled={isSubmitting}>
            {isSubmitting ? 'Analyzing…' : 'Analyze meeting →'}
          </button>
        </div>
      </form>
    </div>
  )
}

function LoginPage({ onAuthenticated }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState(() => localStorage.getItem('meetmind_remembered_email') || '')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [rememberMe, setRememberMe] = useState(true)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [googleLoading, setGoogleLoading] = useState(false)
  const [googleReady, setGoogleReady] = useState(false)
  const googleButtonRef = useRef(null)

  const googleClientId = (import.meta.env.VITE_GOOGLE_CLIENT_ID || '').trim()

  const handleGoogleCredential = useCallback(async (response) => {
    setError('')
    setGoogleLoading(true)
    try {
      const backendUrl = getApiUrl()
      if (!backendUrl) {
        throw new Error(API_NOT_CONFIGURED)
      }
      if (!response?.credential) {
        throw new Error('Google sign-in was cancelled or failed.')
      }
      const result = await authFetch(`${backendUrl}/auth/google`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential: response.credential }),
      }).then(async (fetchResponse) => {
        const data = await fetchResponse.json().catch(() => ({}))
        if (!fetchResponse.ok) {
          throw new Error(data.detail || 'Google sign-in failed.')
        }
        return data
      })
      onAuthenticated(result.user)
    } catch (googleError) {
      setError(googleError.message || 'Google sign-in failed.')
    } finally {
      setGoogleLoading(false)
    }
  }, [onAuthenticated])

  useEffect(() => {
    if (!googleClientId) return
    let cancelled = false
    const setup = () => {
      if (cancelled || !window.google?.accounts?.id) return false
      window.google.accounts.id.initialize({
        client_id: googleClientId,
        callback: handleGoogleCredential,
        auto_select: false,
        cancel_on_tap_outside: true,
      })
      setGoogleReady(true)
      return true
    }
    if (setup()) {
      return () => {
        cancelled = true
      }
    }
    const timer = window.setInterval(() => {
      if (setup()) window.clearInterval(timer)
    }, 100)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [googleClientId, handleGoogleCredential])

  useEffect(() => {
    if (!googleReady || !googleButtonRef.current || !window.google?.accounts?.id) return
    googleButtonRef.current.innerHTML = ''
    window.google.accounts.id.renderButton(googleButtonRef.current, {
      type: 'standard',
      theme: 'outline',
      size: 'large',
      text: mode === 'register' ? 'signup_with' : 'signin_with',
      shape: 'rectangular',
      logo_alignment: 'left',
      width: 320,
    })
  }, [googleReady, mode])

  function switchMode(nextMode) {
    setMode(nextMode)
    setError('')
    setPassword('')
    setDisplayName('')
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError('')
    if (!email.trim()) {
      setError('Enter your email address.')
      return
    }
    if (!password) {
      setError('Enter your password.')
      return
    }
    if (mode === 'register' && password.length < 8) {
      setError('Use at least 8 characters for your password.')
      return
    }
    setLoading(true)
    try {
      const backendUrl = getApiUrl()
      if (!backendUrl) {
        throw new Error(API_NOT_CONFIGURED)
      }
      const endpoint = mode === 'register' ? '/auth/register' : '/auth/login'
      const body = mode === 'register'
        ? { email: email.trim(), password, display_name: displayName.trim() }
        : { email: email.trim(), password }
      const response = await authFetch(`${backendUrl}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const result = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(result.detail || (mode === 'register' ? 'Unable to create account.' : 'Unable to sign in.'))
      }
      if (rememberMe) {
        localStorage.setItem('meetmind_remembered_email', email.trim())
      } else {
        localStorage.removeItem('meetmind_remembered_email')
      }
      onAuthenticated(result.user)
    } catch (loginError) {
      setError(loginError.message || 'Unable to sign in.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <section className="login-panel">
        <div className="login-brand">
          <div className="brand-mark">M</div>
          <span>meetmind</span>
        </div>
        <div className="login-heading">
          <p className="eyebrow">{mode === 'register' ? 'CREATE WORKSPACE ACCOUNT' : 'WORKSPACE SIGN IN'}</p>
          <h1>{mode === 'register' ? 'Get started.' : 'Welcome back.'}</h1>
          <p>{mode === 'register' ? 'Create an account to turn meetings into decisions and execution.' : 'Sign in to turn meetings into decisions and execution.'}</p>
        </div>
        <form className="login-form" onSubmit={handleSubmit}>
          {mode === 'register' && (
            <>
              <label htmlFor="login-name">Name</label>
              <input
                id="login-name"
                type="text"
                autoComplete="name"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="Your name"
                maxLength={100}
                disabled={loading}
              />
            </>
          )}
          <label htmlFor="login-email">Email</label>
          <input
            id="login-email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="name@company.com"
            disabled={loading}
          />
          <label htmlFor="login-password">Password</label>
          <div className="password-field">
            <input
              id="login-password"
              type={showPassword ? 'text' : 'password'}
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="Enter your password"
              disabled={loading}
            />
            <button
              type="button"
              onClick={() => setShowPassword((visible) => !visible)}
              aria-label={showPassword ? 'Hide password' : 'Show password'}
              disabled={loading}
            >
              {showPassword ? 'Hide' : 'Show'}
            </button>
          </div>
          <label className="remember-row">
            <input
              type="checkbox"
              checked={rememberMe}
              onChange={(event) => setRememberMe(event.target.checked)}
              disabled={loading}
            />
            <span>Remember me</span>
          </label>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button type="submit" className="login-submit" disabled={loading}>
            {loading
              ? (mode === 'register' ? 'Creating account…' : 'Signing in…')
              : (mode === 'register' ? 'Create account' : 'Sign in')}
          </button>
          <button
            type="button"
            className="login-switch"
            onClick={() => switchMode(mode === 'register' ? 'login' : 'register')}
            disabled={loading}
          >
            {mode === 'register' ? 'Already have an account? Sign in' : 'New here? Create an account'}
          </button>
          {googleClientId && (
            <div className="google-signin">
              <div className="auth-divider"><span>or</span></div>
              {googleReady ? (
                <div ref={googleButtonRef} className="google-button-container" />
              ) : (
                <button type="button" className="google-fallback-button" disabled>
                  Continue with Google
                </button>
              )}
              {googleLoading && (
                <div className="form-message" role="status">Signing in with Google…</div>
              )}
            </div>
          )}
        </form>
      </section>
      <aside className="login-aside">
        <div className="login-aside-content">
          <span className="login-aside-kicker">MEETMIND INTELLIGENCE</span>
          <h2>From conversation to coordinated action.</h2>
          <p>Decisions, commitments and execution risks remain connected across every meeting.</p>
        </div>
      </aside>
    </div>
  )
}

function App() {
  const [user, setUser] = useState(null)
  const [authChecking, setAuthChecking] = useState(true)

  useEffect(() => {
    const backendUrl = getApiUrl()
    if (!backendUrl) {
      setAuthChecking(false)
      return
    }
    authFetch(`${backendUrl}/auth/me`)
      .then((response) => {
        if (!response.ok) throw new Error('Unauthorized')
        return response.json()
      })
      .then((result) => setUser(result.user))
      .catch(() => setUser(null))
      .finally(() => setAuthChecking(false))
  }, [])

  useEffect(() => {
    function handleUnauthorized() {
      setUser(null)
    }
    window.addEventListener('meetmind:unauthorized', handleUnauthorized)
    return () => window.removeEventListener('meetmind:unauthorized', handleUnauthorized)
  }, [])

  async function handleLogout() {
    const backendUrl = getApiUrl()
    await authFetch(`${backendUrl}/auth/logout`, { method: 'POST' }).catch(() => null)
    setUser(null)
  }

  if (authChecking) {
    return (
      <div className="auth-loading">
        <div className="brand-mark">M</div>
        <p>Checking your workspace…</p>
      </div>
    )
  }

  if (!user) {
    return <LoginPage onAuthenticated={setUser} />
  }

  return <Workspace user={user} onLogout={handleLogout} />
}

function Workspace({ user, onLogout }) {
  const [activePage, setActivePage] = useState('overview')
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [backendStatus, setBackendStatus] = useState('Checking...')
  const [workspaceData, setWorkspaceData] = useState({
    meetings: [],
    decisions: [],
    actions: [],
    drift: [],
    risk: [],
    agentRuns: [],
    toolExecutions: [],
    approvals: [],
  })

  useEffect(() => {
    const backendUrl = getApiUrl()
    authFetch(`${backendUrl}/`)
      .then((response) => response.json())
      .then((data) => {
        setBackendStatus(data.message)
      })
      .catch(() => {
        setBackendStatus('Backend connection failed')
      })

    Promise.all([
      authFetch(`${backendUrl}/meetings`).then((response) => response.json()).catch(() => []),
      authFetch(`${backendUrl}/decisions`).then((response) => response.json()).catch(() => []),
      authFetch(`${backendUrl}/actions`).then((response) => response.json()).catch(() => []),
      authFetch(`${backendUrl}/drift`).then((response) => response.json()).catch(() => []),
      authFetch(`${backendUrl}/risk`).then((response) => response.json()).catch(() => []),
      authFetch(`${backendUrl}/agent/runs`).then((response) => response.json()).catch(() => []),
      authFetch(`${backendUrl}/agent/tool-executions`).then((response) => response.json()).catch(() => []),
      authFetch(`${backendUrl}/approvals`).then((response) => response.json()).catch(() => []),
    ]).then(([meetings, decisions, actions, drift, risk, agentRuns, toolExecutions, approvals]) => {
      setWorkspaceData({ meetings, decisions, actions, drift, risk, agentRuns, toolExecutions, approvals })
    })

    function refreshData() {
      Promise.all([
        authFetch(`${backendUrl}/meetings`).then((response) => response.json()).catch(() => []),
        authFetch(`${backendUrl}/decisions`).then((response) => response.json()).catch(() => []),
        authFetch(`${backendUrl}/actions`).then((response) => response.json()).catch(() => []),
        authFetch(`${backendUrl}/drift`).then((response) => response.json()).catch(() => []),
        authFetch(`${backendUrl}/risk`).then((response) => response.json()).catch(() => []),
        authFetch(`${backendUrl}/agent/runs`).then((response) => response.json()).catch(() => []),
        authFetch(`${backendUrl}/agent/tool-executions`).then((response) => response.json()).catch(() => []),
        authFetch(`${backendUrl}/approvals`).then((response) => response.json()).catch(() => []),
      ]).then(([meetings, decisions, actions, drift, risk, agentRuns, toolExecutions, approvals]) => {
        setWorkspaceData({ meetings, decisions, actions, drift, risk, agentRuns, toolExecutions, approvals })
      })
    }

    window.addEventListener('meetmind:data-refresh', refreshData)
    return () => window.removeEventListener('meetmind:data-refresh', refreshData)
  }, [])

  function navigateTo(page) {
    setActivePage(page)
    setMobileMenuOpen(false)
  }

  return (
    <div className="app-shell">
      {mobileMenuOpen && (
        <button
          className="sidebar-overlay"
          type="button"
          aria-label="Close navigation"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}
      <aside className={`sidebar ${mobileMenuOpen ? 'mobile-open' : ''}`}>
        <div className="brand">
          <div className="brand-mark">M</div>
          <span>meetmind</span>
        </div>

        {navigation.map((section) => (
          <div className="nav-section" key={section.heading}>
            <div className="workspace-label">{section.heading}</div>

            <nav className="navigation">
              {section.items.map((item) => (
                <button
                  key={item.id}
                  className={`nav-item ${activePage === item.id ? 'active' : ''}`}
                  onClick={() => navigateTo(item.id)}
                >
                  <span>{item.icon}</span>
                  {item.label}
                </button>
              ))}
            </nav>
          </div>
        ))}

        <div className="sidebar-bottom">
          <div className="user-avatar">A</div>
          <div>
            <div className="user-name">{user?.display_name || 'MeetMind user'}</div>
            <div className="user-role">{user?.role || 'Workspace admin'}</div>
          </div>
          <button
            type="button"
            className="logout-button"
            onClick={onLogout}
            title="Log out"
          >
            Log out
          </button>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <button
            className="mobile-menu-button"
            type="button"
            aria-label="Open navigation"
            aria-expanded={mobileMenuOpen}
            onClick={() => setMobileMenuOpen(true)}
          >
            <span />
            <span />
            <span />
          </button>
          <div className="breadcrumb">
            Workspace <span>/</span>{' '}
            {activePage === 'overview'
              ? 'Overview'
              : pageDetails[activePage].title}
          </div>

          <div className="topbar-right">
            <span className="system-status">
              <span className="status-dot"></span>
              Backend: {backendStatus}
            </span>
            <span className="system-status">
              <span className="status-dot"></span>
              All systems normal
            </span>
            <button className="avatar-button">A</button>
          </div>
        </header>

        {activePage === 'overview' ? (
          <div className="page-content">
            <section className="welcome-section">
              <div>
                <p className="eyebrow">SATURDAY, NOVEMBER 21</p>
                <h1>Good afternoon, Anusha.</h1>
                <p className="welcome-subtitle">
                  Here's what's happening across your workspace.
                </p>
              </div>
              <button
                className="primary-button"
                onClick={() => navigateTo('meetings')}
              >
                ＋ New meeting
              </button>
            </section>

            <section className="stats-grid">
              <div className="stat-card">
                <div className="stat-heading">Meetings processed <span>↗</span></div>
                <div className="stat-value">{workspaceData.meetings.length}</div>
                <div className="stat-foot">Across your workspace</div>
              </div>
              <div className="stat-card">
                <div className="stat-heading">Decisions captured <span>◇</span></div>
                <div className="stat-value">{workspaceData.decisions.length}</div>
                <div className="stat-foot">From meeting discussions</div>
              </div>
              <div className="stat-card">
                <div className="stat-heading">Open action items <span>✓</span></div>
                <div className="stat-value">{workspaceData.actions.filter((item) => item.status !== 'Done').length}</div>
                <div className="stat-foot">Require follow-through</div>
              </div>
              <div className="stat-card">
                <div className="stat-heading">Potential conflicts <span>⌁</span></div>
                <div className="stat-value conflict-value">{workspaceData.drift.length}</div>
                <div className="stat-foot">Need your attention</div>
              </div>
            </section>

            <section className="content-grid">
              <div className="panel meetings-panel">
                <div className="panel-header">
                  <div>
                    <h2>Recent meetings</h2>
                    <p>Your latest conversations and their outcomes.</p>
                  </div>
                  <button className="text-button" onClick={() => navigateTo('meetings')}>
                    View all →
                  </button>
                </div>

                <div className="meeting-list">
                  {workspaceData.meetings.slice(0, 5).map((meeting) => (
                    <div className="meeting-row" key={meeting.id}>
                      <div className="meeting-icon">▤</div>
                      <div className="meeting-info">
                        <h3>{meeting.title}</h3>
                        <p>{meeting.created_at}</p>
                      </div>
                      <div className="meeting-meta">
                        <span>{meeting.decision_count} decisions</span>
                        <span>{meeting.action_count} tasks</span>
                      </div>
                      <span className="processed-badge">Processed</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="panel actions-panel">
                <div className="panel-header">
                  <div>
                    <h2>Action items</h2>
                    <p>Things that need to get done.</p>
                  </div>
                  <button className="text-button" onClick={() => navigateTo('actions')}>
                    View all →
                  </button>
                </div>

                <div className="action-list">
                  {workspaceData.risk.slice(0, 5).map((action) => (
                    <div className="action-row" key={action.id}>
                      <div className="action-checkbox"></div>
                      <div className="action-info">
                        <h3>{action.text}</h3>
                        <p>{action.owner || 'Owner unclear'} · Due {action.deadline || 'Unclear'}</p>
                      </div>
                      <span className={`priority ${action.risk.toLowerCase()}`}>
                        {action.risk}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </section>

            <section className="insight-panel">
              <div className="insight-icon">✳</div>
              <div className="insight-copy">
                <span className="eyebrow">MEETMIND INTELLIGENCE</span>
                <h2>Your meetings should lead to action, not confusion.</h2>
                <p>
                  MeetMind connects decisions, commitments and follow-ups
                  across your team's conversations.
                </p>
              </div>
              <div className="insight-tag">AI-powered workspace</div>
            </section>

            <footer className="footer">
              <span>MeetMind · Turning conversations into execution.</span>
              <span>Prototype v2.0</span>
            </footer>
          </div>
        ) : activePage === 'meetings' ? (
          <MeetingInput />
        ) : (
          <div className="page-content">
            <section className="subpage-heading">
              <p className="eyebrow">{pageDetails[activePage].eyebrow}</p>
              <h1>{pageDetails[activePage].title}</h1>
              <p className="welcome-subtitle">
                {pageDetails[activePage].description}
              </p>
            </section>

            {activePage === 'meetings' && (
              <section className="panel meeting-list-panel">
                <div className="list-panel-header">
                  <div>
                    <h2>Meeting library</h2>
                    <p>Processed conversations and outcomes</p>
                  </div>
                  <span className="list-count">{workspaceData.meetings.length} meetings</span>
                </div>
                {workspaceData.meetings.length === 0 ? (
                  <div className="empty-state"><h2>No meetings yet</h2><p>Analyze a transcript to create the first meeting record.</p></div>
                ) : workspaceData.meetings.map((meeting) => (
                  <div className="meeting-row" key={meeting.id}>
                    <div className="meeting-icon">▤</div>
                    <div className="meeting-info"><h3>{meeting.title}</h3><p>{meeting.created_at}</p></div>
                    <div className="meeting-meta"><span>{meeting.decision_count} decisions</span><span>{meeting.action_count} actions</span></div>
                  </div>
                ))}
              </section>
            )}

            {activePage === 'decisions' && (
              <section className="panel meeting-list-panel">
                <div className="list-panel-header">
                  <div>
                    <h2>Decision record</h2>
                    <p>Traceable agreements from processed meetings</p>
                  </div>
                  <span className="list-count">{workspaceData.decisions.length} decisions</span>
                </div>
                {workspaceData.decisions.map((decision) => (
                  <div className="meeting-row" key={decision.id}>
                    <div className="meeting-icon">◇</div>
                    <div className="meeting-info"><h3>{decision.text}</h3><p>{decision.meeting_title} · Source: “{decision.source_passage}”</p></div>
                  </div>
                ))}
              </section>
            )}

            {activePage === 'actions' && (
              <section className="panel meeting-list-panel">
                <div className="list-panel-header">
                  <div>
                    <h2>Commitment queue</h2>
                    <p>Ownership, deadlines and risk status</p>
                  </div>
                  <span className="list-count">{workspaceData.risk.length} open items</span>
                </div>
                {workspaceData.risk.map((action) => (
                  <div className="meeting-row" key={action.id}>
                    <div className="meeting-icon">✓</div>
                    <div className="meeting-info"><h3>{action.text}</h3><p>{action.owner || 'Owner unclear'} · {action.deadline || 'Deadline unclear'} · {action.meeting_title}</p></div>
                    <span className={`priority ${action.risk.toLowerCase()}`}>{action.risk}: {action.reason}</span>
                  </div>
                ))}
              </section>
            )}

            {activePage === 'drift' && (
              <section className="panel meeting-list-panel">
                <div className="list-panel-header">
                  <div>
                    <h2>Decision drift</h2>
                    <p>Potential contradictions requiring review</p>
                  </div>
                  <span className="list-count">{workspaceData.drift.length} conflicts</span>
                </div>
                {workspaceData.drift.length === 0 ? (
                  <div className="empty-state"><h2>No decision drift detected</h2><p>New conflicts will appear here when contrasting decisions are found.</p></div>
                ) : workspaceData.drift.map((conflict) => (
                  <div className="meeting-row" key={conflict.id}>
                    <div className="meeting-icon">⌁</div>
                    <div className="meeting-info"><h3>{conflict.reason}</h3><p>{conflict.previous_meeting}: “{conflict.previous_decision}” → “{conflict.new_decision}”</p></div>
                  </div>
                ))}
              </section>
            )}

            {activePage === 'memory' && (
              <section className="panel meeting-list-panel">
                <div className="list-panel-header">
                  <div>
                    <h2>Team context</h2>
                    <p>Decisions and source passages retained across meetings</p>
                  </div>
                  <span className="list-count">{workspaceData.decisions.length} memories</span>
                </div>
                {workspaceData.decisions.length === 0 ? (
                  <div className="empty-state"><h2>Team memory is empty</h2><p>Decisions extracted from meetings become durable team context.</p></div>
                ) : workspaceData.decisions.map((decision) => (
                  <div className="meeting-row" key={decision.id}>
                    <div className="meeting-icon">▧</div>
                    <div className="meeting-info"><h3>{decision.text}</h3><p>{decision.meeting_title} · Traceable source: “{decision.source_passage}”</p></div>
                  </div>
                ))}
              </section>
            )}

            {activePage === 'activity' && (
              <AgentActivity
                runs={workspaceData.agentRuns}
                toolExecutions={workspaceData.toolExecutions}
                approvals={workspaceData.approvals}
              />
            )}

            {activePage === 'approvals' && (
              <ApprovalCenter
                requests={workspaceData.approvals}
                onChanged={() => window.dispatchEvent(new Event('meetmind:data-refresh'))}
              />
            )}

            <footer className="footer">
              <span>MeetMind · Turning conversations into execution.</span>
              <span>Prototype v2.0</span>
            </footer>
          </div>
        )}
      </main>
    </div>
  )
}

export default App
