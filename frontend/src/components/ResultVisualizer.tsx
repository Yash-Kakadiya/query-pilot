import React, { useMemo, useState } from 'react'
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  LineChart,
  Line,
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from 'recharts'
import type { ExecuteQueryResponse } from '../types/api'
import './ResultVisualizer.css'

export type ChartType = 'bar' | 'line' | 'scatter'

export interface ResultVisualizerProps {
  result: ExecuteQueryResponse
}

interface ColumnAnalysis {
  name: string
  numericCount: number
  temporalCount: number
  stringCount: number
  nullCount: number
  totalCount: number
  isNumeric: boolean
  isTemporal: boolean
  isCategorical: boolean
}

const MAX_POINTS_LINE = 200
const MAX_POINTS_SCATTER = 200
const MAX_POINTS_BAR = 100
const ISO_DATE_PATTERN = /^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}/

import { sampleUniformStride } from '../utils/visualizerSampling.ts'

export { sampleUniformStride }

/**
 * Analyzes column values across returned rows to classify them as numeric, temporal, or categorical.
 */
function analyzeColumns(columns: string[], rows: Record<string, unknown>[]): Record<string, ColumnAnalysis> {
  const analysis: Record<string, ColumnAnalysis> = {}

  for (const col of columns) {
    analysis[col] = {
      name: col,
      numericCount: 0,
      temporalCount: 0,
      stringCount: 0,
      nullCount: 0,
      totalCount: rows.length,
      isNumeric: false,
      isTemporal: false,
      isCategorical: false,
    }
  }

  const sampleRows = rows.slice(0, 100)
  for (const row of sampleRows) {
    for (const col of columns) {
      const val = row[col]
      if (val === null || val === undefined || val === '') {
        analysis[col].nullCount++
        continue
      }

      if (typeof val === 'number') {
        if (Number.isFinite(val)) {
          analysis[col].numericCount++
        }
        continue
      }

      const strVal = String(val).trim()
      const num = Number(strVal)
      if (!isNaN(num) && Number.isFinite(num)) {
        analysis[col].numericCount++
        continue
      }

      if (ISO_DATE_PATTERN.test(strVal) || !isNaN(Date.parse(strVal))) {
        analysis[col].temporalCount++
        continue
      }

      analysis[col].stringCount++
    }
  }

  const sampleSize = Math.max(1, sampleRows.length)
  for (const col of columns) {
    const a = analysis[col]
    const nonNullCount = sampleSize - a.nullCount
    if (nonNullCount <= 0) {
      a.isCategorical = true
      continue
    }

    if (a.numericCount / nonNullCount >= 0.7) {
      a.isNumeric = true
    } else if (a.temporalCount / nonNullCount >= 0.7) {
      a.isTemporal = true
    } else {
      a.isCategorical = true
    }
  }

  return analysis
}

export function ResultVisualizer({ result }: ResultVisualizerProps) {
  const { columns, rows } = result

  // 1. Analyze column data types
  const columnAnalysis = useMemo(() => analyzeColumns(columns, rows), [columns, rows])

  // 2. Identify candidate axes
  const numericColumns = useMemo(
    () => columns.filter((col) => columnAnalysis[col]?.isNumeric),
    [columns, columnAnalysis]
  )
  const temporalColumns = useMemo(
    () => columns.filter((col) => columnAnalysis[col]?.isTemporal),
    [columns, columnAnalysis]
  )
  const categoricalColumns = useMemo(
    () => columns.filter((col) => columnAnalysis[col]?.isCategorical),
    [columns, columnAnalysis]
  )

  // 3. Determine best initial auto-detected chart configuration
  const initialConfig = useMemo(() => {
    if (numericColumns.length === 0) {
      return null
    }

    if (temporalColumns.length > 0 && numericColumns.length > 0) {
      return {
        type: 'line' as ChartType,
        xCol: temporalColumns[0],
        yCol: numericColumns[0],
      }
    }

    if (categoricalColumns.length > 0 && numericColumns.length > 0) {
      return {
        type: 'bar' as ChartType,
        xCol: categoricalColumns[0],
        yCol: numericColumns[0],
      }
    }

    if (numericColumns.length >= 2) {
      return {
        type: 'scatter' as ChartType,
        xCol: numericColumns[0],
        yCol: numericColumns[1],
      }
    }

    // Default: 1 numeric column with row index as X
    return {
      type: 'bar' as ChartType,
      xCol: columns[0],
      yCol: numericColumns[0],
    }
  }, [columns, numericColumns, temporalColumns, categoricalColumns])

  const [chartType, setChartType] = useState<ChartType>(initialConfig?.type || 'bar')
  const [selectedX, setSelectedX] = useState<string>(initialConfig?.xCol || columns[0] || '')
  const [selectedY, setSelectedY] = useState<string>(initialConfig?.yCol || numericColumns[0] || '')

  React.useEffect(() => {
    if (initialConfig) {
      setChartType(initialConfig.type)
      setSelectedX(initialConfig.xCol)
      setSelectedY(initialConfig.yCol)
    }
  }, [initialConfig])

  // 4. Guard against unchartable data
  if (rows.length === 0) {
    return (
      <div className="visualizer-empty" role="status">
        <span className="visualizer-empty-icon" aria-hidden="true">📉</span>
        <p className="visualizer-empty-title">No Data Returned</p>
        <p className="visualizer-empty-desc">The query returned zero records. Execute a query with data rows to render a chart.</p>
      </div>
    )
  }

  if (columns.length < 2 && numericColumns.length === 0) {
    return (
      <div className="visualizer-empty" role="status">
        <span className="visualizer-empty-icon" aria-hidden="true">📊</span>
        <p className="visualizer-empty-title">Insufficient Columns for Visualization</p>
        <p className="visualizer-empty-desc">Visualizations require at least two columns or a quantitative numeric metric.</p>
      </div>
    )
  }

  if (numericColumns.length === 0) {
    return (
      <div className="visualizer-empty" role="status">
        <span className="visualizer-empty-icon" aria-hidden="true">🔢</span>
        <p className="visualizer-empty-title">No Numeric Metrics Available</p>
        <p className="visualizer-empty-desc">
          Charts require at least one quantitative numeric column (e.g., counts, amounts, percentages). All returned columns appear to be non-numeric text.
        </p>
      </div>
    )
  }

  // 5. Validation of manual selection
  const isSelectedYNumeric = columnAnalysis[selectedY]?.isNumeric
  const isSelectedXNumeric = columnAnalysis[selectedX]?.isNumeric
  const isScatterSuitable = isSelectedXNumeric && isSelectedYNumeric

  // 6. Deterministic Data Reduction Policy
  let displayRows = rows
  let reductionNotice: string | null = null

  if (chartType === 'line') {
    if (rows.length > MAX_POINTS_LINE) {
      displayRows = sampleUniformStride(rows, MAX_POINTS_LINE)
      reductionNotice = `Displaying ${MAX_POINTS_LINE} points uniformly sampled across all ${rows.length} rows to preserve chronological span. Complete dataset preserved in Table View.`
    }
  } else if (chartType === 'scatter') {
    if (rows.length > MAX_POINTS_SCATTER) {
      displayRows = sampleUniformStride(rows, MAX_POINTS_SCATTER)
      reductionNotice = `Displaying ${MAX_POINTS_SCATTER} rows uniformly sampled across all ${rows.length} rows. Complete dataset preserved in Table View.`
    }
  } else {
    // Bar chart (categorical)
    if (rows.length > MAX_POINTS_BAR) {
      displayRows = sampleUniformStride(rows, MAX_POINTS_BAR)
      reductionNotice = `Displaying ${MAX_POINTS_BAR} categories uniformly sampled across all ${rows.length} total categories. Complete dataset preserved in Table View.`
    }
  }

  // 7. Missing Value & Gap Handling
  let missingValueCount = 0
  let omittedScatterPoints = 0

  const chartData: Array<{ name: string; x: any; y: number | null; rawY: unknown; index: number }> = []
  const scatterData: Array<{ x: number; y: number; name: string; index: number }> = []

  for (let idx = 0; idx < displayRows.length; idx++) {
    const row = displayRows[idx]
    const rawX = row[selectedX]
    const rawY = row[selectedY]

    if (chartType === 'scatter') {
      const isXZero = (rawX === 0 || rawX === '0') && typeof rawX !== 'boolean'
      const isYZero = (rawY === 0 || rawY === '0') && typeof rawY !== 'boolean'
      const numX = typeof rawX === 'number' ? rawX : (typeof rawX === 'string' && rawX.trim() !== '' ? Number(rawX) : NaN)
      const numY = typeof rawY === 'number' ? rawY : (typeof rawY === 'string' && rawY.trim() !== '' ? Number(rawY) : NaN)

      const isXValid = (isXZero || Number.isFinite(numX)) && rawX !== null && rawX !== undefined && rawX !== '' && typeof rawX !== 'boolean'
      const isYValid = (isYZero || Number.isFinite(numY)) && rawY !== null && rawY !== undefined && rawY !== '' && typeof rawY !== 'boolean'

      if (!isXValid || !isYValid) {
        omittedScatterPoints++
        continue
      }

      scatterData.push({
        x: isXZero ? 0 : numX,
        y: isYZero ? 0 : numY,
        name: `Row ${idx + 1}`,
        index: idx + 1,
      })
    } else {
      let yVal: number | null = null

      if ((rawY === 0 || rawY === '0') && typeof rawY !== 'boolean') {
        // Preserves actual numeric 0
        yVal = 0
      } else if (rawY === null || rawY === undefined || rawY === '' || typeof rawY === 'boolean') {
        // Missing value: represent as chart gap, never falsify as 0
        missingValueCount++
        yVal = null
      } else {
        const num = typeof rawY === 'number' ? rawY : (typeof rawY === 'string' && rawY.trim() !== '' ? Number(rawY) : NaN)
        if (Number.isFinite(num)) {
          yVal = num
        } else {
          // Invalid number
          missingValueCount++
          yVal = null
        }
      }

      const displayX = rawX !== null && rawX !== undefined ? String(rawX) : `Row ${idx + 1}`
      chartData.push({
        name: displayX,
        x: rawX,
        y: yVal,
        rawY,
        index: idx + 1,
      })
    }
  }

  return (
    <div className="visualizer-container" role="region" aria-label="Interactive Query Result Visualization">
      {/* Visualizer Controls Header */}
      <div className="visualizer-toolbar">
        <div className="visualizer-type-selector" role="group" aria-label="Chart type selector">
          <button
            type="button"
            className={`visualizer-type-btn ${chartType === 'bar' ? 'active' : ''}`}
            onClick={() => setChartType('bar')}
          >
            📊 Bar
          </button>
          <button
            type="button"
            className={`visualizer-type-btn ${chartType === 'line' ? 'active' : ''}`}
            onClick={() => setChartType('line')}
          >
            📈 Line
          </button>
          <button
            type="button"
            className={`visualizer-type-btn ${chartType === 'scatter' ? 'active' : ''}`}
            onClick={() => setChartType('scatter')}
            disabled={!isScatterSuitable}
            title={!isScatterSuitable ? 'Scatter charts require numeric values on both X and Y axes' : undefined}
          >
            ⚪ Scatter
          </button>
        </div>

        <div className="visualizer-axis-selectors">
          <div className="visualizer-axis-group">
            <label htmlFor="vis-x-axis">X-Axis:</label>
            <select
              id="vis-x-axis"
              className="visualizer-select"
              value={selectedX}
              onChange={(e) => setSelectedX(e.target.value)}
            >
              {columns.map((col) => (
                <option key={col} value={col}>
                  {col} {columnAnalysis[col]?.isNumeric ? '(num)' : columnAnalysis[col]?.isTemporal ? '(date)' : ''}
                </option>
              ))}
            </select>
          </div>

          <div className="visualizer-axis-group">
            <label htmlFor="vis-y-axis">Y-Axis (Metric):</label>
            <select
              id="vis-y-axis"
              className="visualizer-select"
              value={selectedY}
              onChange={(e) => setSelectedY(e.target.value)}
            >
              {columns.map((col) => (
                <option key={col} value={col}>
                  {col} {columnAnalysis[col]?.isNumeric ? '(num)' : ''}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Manual Selection Validation Warnings */}
      {!isSelectedYNumeric && (
        <div className="visualizer-warning-banner" role="alert">
          ⚠️ Selected Y-Axis column <strong>"{selectedY}"</strong> contains non-numeric data. Please select a quantitative numeric column to plot values.
        </div>
      )}
      {chartType === 'scatter' && !isSelectedXNumeric && (
        <div className="visualizer-warning-banner" role="alert">
          ⚠️ Scatter plots require numeric coordinates on both axes. Column <strong>"{selectedX}"</strong> contains non-numeric data.
        </div>
      )}

      {/* Notices for Data Reduction or Missing Values */}
      {(reductionNotice || missingValueCount > 0 || omittedScatterPoints > 0) && (
        <div className="visualizer-notices">
          {reductionNotice && (
            <span className="visualizer-notice-chip" role="status">
              ⚡ {reductionNotice}
            </span>
          )}
          {missingValueCount > 0 && (
            <span className="visualizer-notice-chip visualizer-warning-chip" role="status">
              ℹ️ {missingValueCount} missing or invalid value(s) in displayed data represented as chart gaps (valid 0s preserved).
            </span>
          )}
          {omittedScatterPoints > 0 && (
            <span className="visualizer-notice-chip visualizer-warning-chip" role="status">
              ℹ️ {omittedScatterPoints} sampled row(s) with non-numeric coordinates excluded (plotting {scatterData.length} valid points).
            </span>
          )}
        </div>
      )}

      {/* Responsive Recharts Display */}
      <div className="visualizer-chart-area">
        <ResponsiveContainer width="100%" height={360}>
          {chartType === 'bar' ? (
            <BarChart data={chartData} margin={{ top: 20, right: 30, left: 20, bottom: 40 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis
                dataKey="name"
                stroke="#64748b"
                fontSize={12}
                interval="preserveStartEnd"
                angle={chartData.length > 15 ? -35 : 0}
                textAnchor={chartData.length > 15 ? 'end' : 'middle'}
              />
              <YAxis stroke="#64748b" fontSize={12} />
              <Tooltip
                contentStyle={{ backgroundColor: '#ffffff', borderRadius: '8px', border: '1px solid #cbd5e1' }}
                formatter={(value: any) => [
                  value === null || value === undefined ? 'Missing (No data)' : value,
                  selectedY,
                ]}
              />
              <Legend verticalAlign="top" height={36} />
              <Bar dataKey="y" name={selectedY} fill="#3b82f6" radius={[4, 4, 0, 0]} />
            </BarChart>
          ) : chartType === 'line' ? (
            <LineChart data={chartData} margin={{ top: 20, right: 30, left: 20, bottom: 40 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis
                dataKey="name"
                stroke="#64748b"
                fontSize={12}
                interval="preserveStartEnd"
                angle={chartData.length > 15 ? -35 : 0}
                textAnchor={chartData.length > 15 ? 'end' : 'middle'}
              />
              <YAxis stroke="#64748b" fontSize={12} />
              <Tooltip
                contentStyle={{ backgroundColor: '#ffffff', borderRadius: '8px', border: '1px solid #cbd5e1' }}
                formatter={(value: any) => [
                  value === null || value === undefined ? 'Missing (No data)' : value,
                  selectedY,
                ]}
              />
              <Legend verticalAlign="top" height={36} />
              <Line
                type="monotone"
                dataKey="y"
                name={selectedY}
                stroke="#2563eb"
                strokeWidth={2}
                connectNulls={false}
                dot={{ r: chartData.length > 50 ? 0 : 3 }}
                activeDot={{ r: 6 }}
              />
            </LineChart>
          ) : (
            <ScatterChart margin={{ top: 20, right: 30, left: 20, bottom: 40 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis
                type="number"
                dataKey="x"
                name={selectedX}
                stroke="#64748b"
                fontSize={12}
                label={{ value: selectedX, position: 'insideBottom', offset: -10 }}
              />
              <YAxis
                type="number"
                dataKey="y"
                name={selectedY}
                stroke="#64748b"
                fontSize={12}
                label={{ value: selectedY, angle: -90, position: 'insideLeft' }}
              />
              <Tooltip
                cursor={{ strokeDasharray: '3 3' }}
                contentStyle={{ backgroundColor: '#ffffff', borderRadius: '8px', border: '1px solid #cbd5e1' }}
              />
              <Legend verticalAlign="top" height={36} />
              <Scatter name={`${selectedX} vs ${selectedY}`} data={scatterData} fill="#8b5cf6" />
            </ScatterChart>
          )}
        </ResponsiveContainer>
      </div>
    </div>
  )
}

export default ResultVisualizer

