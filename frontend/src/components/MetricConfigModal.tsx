import React, { useState, useId } from 'react'
import type { BusinessMetricDefinition } from '../types/api'
import './MetricConfigModal.css'

export interface MetricConfigModalProps {
  isOpen: boolean
  onClose: () => void
  metrics: BusinessMetricDefinition[]
  availableTables: string[]
  onSaveMetrics: (metrics: BusinessMetricDefinition[]) => void
}

const MAX_METRICS = 5
const MAX_NAME_LEN = 100
const MAX_DESC_LEN = 500
const MAX_FORMULA_LEN = 500
const MAX_TABLES = 10
const MAX_PAYLOAD_BYTES = 8192

function measurePayloadBytes(metrics: BusinessMetricDefinition[]): number {
  try {
    return new TextEncoder().encode(JSON.stringify(metrics)).length
  } catch {
    return 0
  }
}

export function MetricConfigModal({
  isOpen,
  onClose,
  metrics,
  availableTables,
  onSaveMetrics,
}: MetricConfigModalProps) {
  const [localMetrics, setLocalMetrics] = useState<BusinessMetricDefinition[]>(metrics)
  const [editingIndex, setEditingIndex] = useState<number | null>(null)

  // Form fields for active editing/adding
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [formula, setFormula] = useState('')
  const [selectedTables, setSelectedTables] = useState<string[]>([])
  const [formError, setFormError] = useState<string | null>(null)

  const nameInputId = useId()
  const descInputId = useId()
  const formulaInputId = useId()

  // Sync local metrics when modal opens
  React.useEffect(() => {
    if (isOpen) {
      setLocalMetrics(metrics)
      resetForm()
    }
  }, [isOpen, metrics])

  if (!isOpen) {
    return null
  }

  function resetForm() {
    setEditingIndex(null)
    setName('')
    setDescription('')
    setFormula('')
    setSelectedTables([])
    setFormError(null)
  }

  function startEdit(idx: number) {
    const item = localMetrics[idx]
    if (!item) return
    setEditingIndex(idx)
    setName(item.name)
    setDescription(item.description)
    setFormula(item.calculation_formula || '')
    setSelectedTables(item.applicable_tables || [])
    setFormError(null)
  }

  function toggleTable(tbl: string) {
    if (selectedTables.includes(tbl)) {
      setSelectedTables(selectedTables.filter((t) => t !== tbl))
    } else {
      if (selectedTables.length >= MAX_TABLES) {
        setFormError(`Cannot select more than ${MAX_TABLES} applicable tables per metric.`)
        return
      }
      setSelectedTables([...selectedTables, tbl])
      setFormError(null)
    }
  }

  function handleSaveCurrent() {
    const trimmedName = name.trim()
    const trimmedDesc = description.trim()
    const trimmedFormula = formula.trim()

    if (!trimmedName) {
      setFormError('Metric name cannot be empty.')
      return
    }
    if (trimmedName.length > MAX_NAME_LEN) {
      setFormError(`Metric name cannot exceed ${MAX_NAME_LEN} characters.`)
      return
    }
    if (!trimmedDesc) {
      setFormError('Metric description cannot be empty.')
      return
    }
    if (trimmedDesc.length > MAX_DESC_LEN) {
      setFormError(`Metric description cannot exceed ${MAX_DESC_LEN} characters.`)
      return
    }
    if (trimmedFormula.length > MAX_FORMULA_LEN) {
      setFormError(`Formula cannot exceed ${MAX_FORMULA_LEN} characters.`)
      return
    }
    if (selectedTables.length > MAX_TABLES) {
      setFormError(`Cannot select more than ${MAX_TABLES} applicable tables per metric.`)
      return
    }

    // Check unique name case-insensitively
    const duplicate = localMetrics.some(
      (m, idx) => idx !== editingIndex && m.name.trim().toLowerCase() === trimmedName.toLowerCase()
    )
    if (duplicate) {
      setFormError(`A metric named "${trimmedName}" already exists (names must be unique).`)
      return
    }

    const newDef: BusinessMetricDefinition = {
      name: trimmedName,
      description: trimmedDesc,
      ...(trimmedFormula ? { calculation_formula: trimmedFormula } : {}),
      ...(selectedTables.length > 0 ? { applicable_tables: selectedTables } : {}),
    }

    let updatedList: BusinessMetricDefinition[]
    if (editingIndex !== null) {
      updatedList = localMetrics.map((m, idx) => (idx === editingIndex ? newDef : m))
    } else {
      if (localMetrics.length >= MAX_METRICS) {
        setFormError(`Maximum of ${MAX_METRICS} business metric definitions allowed.`)
        return
      }
      updatedList = [...localMetrics, newDef]
    }

    const totalBytes = measurePayloadBytes(updatedList)
    if (totalBytes > MAX_PAYLOAD_BYTES) {
      setFormError(`Total metric definitions payload (${totalBytes} bytes) exceeds limit of ${MAX_PAYLOAD_BYTES} bytes.`)
      return
    }

    setLocalMetrics(updatedList)
    resetForm()
  }

  function handleDelete(idx: number) {
    const updated = localMetrics.filter((_, i) => i !== idx)
    setLocalMetrics(updated)
    if (editingIndex === idx) {
      resetForm()
    }
  }

  function handleApplyAll() {
    const totalBytes = measurePayloadBytes(localMetrics)
    if (totalBytes > MAX_PAYLOAD_BYTES) {
      setFormError(`Payload (${totalBytes} bytes) exceeds limit of ${MAX_PAYLOAD_BYTES} bytes.`)
      return
    }
    onSaveMetrics(localMetrics)
    onClose()
  }

  return (
    <div className="metric-modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="metric-modal-title">
      <div className="metric-modal-container">
        <div className="metric-modal-header">
          <div className="metric-modal-title-group">
            <span className="metric-modal-icon" aria-hidden="true">📐</span>
            <div>
              <h2 id="metric-modal-title" className="metric-modal-title">Business Metric Definitions</h2>
              <p className="metric-modal-subtitle">
                Configure request-scoped business metrics to guide SQL generation with domain formulas.
              </p>
            </div>
          </div>
          <button
            type="button"
            className="metric-modal-close-btn"
            onClick={onClose}
            aria-label="Close Business Metrics modal"
          >
            ✕
          </button>
        </div>

        <div className="metric-modal-banner" role="note">
          <span className="metric-banner-icon" aria-hidden="true">💡</span>
          <p>
            <strong>Informational Guidance:</strong> Business metrics guide the SQL generator with business definitions and aggregation formulas. They do <em>not</em> execute raw SQL or bypass database access controls.
          </p>
        </div>

        <div className="metric-modal-body">
          {/* Left panel: List of configured metrics */}
          <div className="metric-list-section">
            <div className="metric-list-header">
              <span className="metric-list-count">
                Configured: <strong>{localMetrics.length}</strong> / {MAX_METRICS}
              </span>
              {editingIndex === null && localMetrics.length < MAX_METRICS && (
                <span className="metric-can-add-hint">Ready to add new definition</span>
              )}
            </div>

            {localMetrics.length === 0 ? (
              <div className="metric-empty-list" role="status">
                No business metrics configured. Add definitions below to guide domain calculations.
              </div>
            ) : (
              <div className="metric-cards-scroll">
                {localMetrics.map((item, idx) => (
                  <div
                    key={idx}
                    className={`metric-item-card ${editingIndex === idx ? 'metric-card-editing' : ''}`}
                  >
                    <div className="metric-card-header">
                      <strong className="metric-card-name">{item.name}</strong>
                      <div className="metric-card-actions">
                        <button
                          type="button"
                          className="metric-btn-small"
                          onClick={() => startEdit(idx)}
                          aria-label={`Edit metric ${item.name}`}
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          className="metric-btn-small metric-btn-delete"
                          onClick={() => handleDelete(idx)}
                          aria-label={`Delete metric ${item.name}`}
                        >
                          Remove
                        </button>
                      </div>
                    </div>
                    <p className="metric-card-desc">{item.description}</p>
                    {item.calculation_formula && (
                      <div className="metric-card-formula">
                        <code>{item.calculation_formula}</code>
                      </div>
                    )}
                    {item.applicable_tables && item.applicable_tables.length > 0 && (
                      <div className="metric-card-tables">
                        {item.applicable_tables.map((tbl, tIdx) => (
                          <span key={tIdx} className="metric-table-chip">
                            {tbl}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Right panel: Editor form */}
          <div className="metric-form-section">
            <h3 className="metric-form-title">
              {editingIndex !== null ? `Edit "${localMetrics[editingIndex]?.name}"` : 'Add New Metric Definition'}
            </h3>

            {formError && (
              <div className="metric-form-error" role="alert">
                ⚠️ {formError}
              </div>
            )}

            <div className="metric-field-group">
              <div className="metric-field-label-row">
                <label htmlFor={nameInputId}>Metric Name *</label>
                <span className="metric-char-count">{name.length}/{MAX_NAME_LEN}</span>
              </div>
              <input
                id={nameInputId}
                type="text"
                className="metric-input"
                placeholder="e.g. Net Revenue, At-Risk Attendance"
                value={name}
                maxLength={MAX_NAME_LEN}
                onChange={(e) => setName(e.target.value)}
                disabled={editingIndex === null && localMetrics.length >= MAX_METRICS}
              />
            </div>

            <div className="metric-field-group">
              <div className="metric-field-label-row">
                <label htmlFor={descInputId}>Business Definition *</label>
                <span className="metric-char-count">{description.length}/{MAX_DESC_LEN}</span>
              </div>
              <textarea
                id={descInputId}
                className="metric-textarea"
                rows={3}
                placeholder="Plain-text explanation of how this business metric is defined..."
                value={description}
                maxLength={MAX_DESC_LEN}
                onChange={(e) => setDescription(e.target.value)}
                disabled={editingIndex === null && localMetrics.length >= MAX_METRICS}
              />
            </div>

            <div className="metric-field-group">
              <div className="metric-field-label-row">
                <label htmlFor={formulaInputId}>Calculation Formula (Optional)</label>
                <span className="metric-char-count">{formula.length}/{MAX_FORMULA_LEN}</span>
              </div>
              <input
                id={formulaInputId}
                type="text"
                className="metric-input"
                placeholder="e.g. SUM(GrossAmount) - SUM(Discounts)"
                value={formula}
                maxLength={MAX_FORMULA_LEN}
                onChange={(e) => setFormula(e.target.value)}
                disabled={editingIndex === null && localMetrics.length >= MAX_METRICS}
              />
            </div>

            <div className="metric-field-group">
              <div className="metric-field-label-row">
                <label>Applicable Tables (Max {MAX_TABLES})</label>
                <span className="metric-char-count">{selectedTables.length}/{MAX_TABLES}</span>
              </div>
              {availableTables.length === 0 ? (
                <p className="metric-no-tables">No schema tables available from active profile.</p>
              ) : (
                <div className="metric-table-checkboxes" role="group" aria-label="Applicable schema tables">
                  {availableTables.map((tbl) => {
                    const isChecked = selectedTables.includes(tbl)
                    return (
                      <button
                        key={tbl}
                        type="button"
                        className={`metric-table-toggle-btn ${isChecked ? 'active' : ''}`}
                        onClick={() => toggleTable(tbl)}
                        aria-pressed={isChecked}
                      >
                        {isChecked ? '✓ ' : '+ '}
                        {tbl}
                      </button>
                    )
                  })}
                </div>
              )}
            </div>

            <div className="metric-form-actions">
              {editingIndex !== null ? (
                <>
                  <button
                    type="button"
                    className="metric-btn-secondary"
                    onClick={resetForm}
                  >
                    Cancel Edit
                  </button>
                  <button
                    type="button"
                    className="metric-btn-primary"
                    onClick={handleSaveCurrent}
                  >
                    Update Metric
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="metric-btn-primary"
                  onClick={handleSaveCurrent}
                  disabled={localMetrics.length >= MAX_METRICS}
                >
                  {localMetrics.length >= MAX_METRICS ? 'Limit Reached (5/5)' : '+ Add Metric'}
                </button>
              )}
            </div>
          </div>
        </div>

        <div className="metric-modal-footer">
          <div className="metric-payload-indicator">
            Payload size: <strong>{measurePayloadBytes(localMetrics)}</strong> / {MAX_PAYLOAD_BYTES} bytes
          </div>
          <div className="metric-footer-buttons">
            <button
              type="button"
              className="metric-btn-secondary"
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="button"
              className="metric-btn-apply"
              onClick={handleApplyAll}
            >
              Apply to Session
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

