import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react'
import type { AgentTableMetadata } from '../../types/api.ts'
import './ERDiagramCanvas.css'

export interface ERDiagramCanvasProps {
  tables: AgentTableMetadata[]
  databaseName?: string
  onPeekTable?: (tableName: string, schemaName?: string) => void
  onInspectTable?: (tableName: string) => void
}

interface TablePosition {
  x: number
  y: number
}

interface ForeignKeyRelation {
  id: string
  sourceTable: string
  sourceCol: string
  targetTable: string
  targetCol: string
}

const TABLE_WIDTH = 250
const HEADER_HEIGHT = 42
const ROW_HEIGHT = 26
const PADDING = 12

export function ERDiagramCanvas({
  tables,
  databaseName = 'Database',
  onPeekTable,
  onInspectTable,
}: ERDiagramCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  // Canvas Viewport State
  const [zoom, setZoom] = useState<number>(0.9)
  const [pan, setPan] = useState<{ x: number; y: number }>({ x: 40, y: 40 })
  const [isPanning, setIsPanning] = useState<boolean>(false)
  const [panStart, setPanStart] = useState<{ x: number; y: number }>({ x: 0, y: 0 })

  // Node Dragging State
  const [positions, setPositions] = useState<Record<string, TablePosition>>({})
  const [draggingTable, setDraggingTable] = useState<string | null>(null)
  const [dragOffset, setDragOffset] = useState<{ x: number; y: number }>({ x: 0, y: 0 })

  // Interactive Hover & Filter State
  const [hoveredTable, setHoveredTable] = useState<string | null>(null)
  const [hoveredFk, setHoveredFk] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState<string>('')
  const [isFullscreen, setIsFullscreen] = useState<boolean>(false)

  // Initialize deterministic layout based on tables count
  useEffect(() => {
    if (!tables || tables.length === 0) return

    setPositions((prev) => {
      const updated: Record<string, TablePosition> = { ...prev }
      const colsPerRow = Math.max(2, Math.ceil(Math.sqrt(tables.length * 1.5)))

      tables.forEach((table, idx) => {
        if (!updated[table.table_name]) {
          const col = idx % colsPerRow
          const row = Math.floor(idx / colsPerRow)
          updated[table.table_name] = {
            x: 50 + col * (TABLE_WIDTH + 80),
            y: 50 + row * 320,
          }
        }
      })
      return updated
    })
  }, [tables])

  // Extract all Foreign Key relationships across tables
  const relations = useMemo<ForeignKeyRelation[]>(() => {
    const list: ForeignKeyRelation[] = []
    if (!tables) return list

    const tableNames = new Set(tables.map((t) => t.table_name.toLowerCase()))

    tables.forEach((table) => {
      const fks = table.foreign_keys || []
      fks.forEach((fk, fkIdx) => {
        if (fk.target_table && tableNames.has(fk.target_table.toLowerCase())) {
          list.push({
            id: `${table.table_name}-${fk.source_column}-${fk.target_table}-${fk.target_column}-${fkIdx}`,
            sourceTable: table.table_name,
            sourceCol: fk.source_column,
            targetTable: fk.target_table,
            targetCol: fk.target_column,
          })
        }
      })
    })
    return list
  }, [tables])

  // Search filtering
  const matchingTableNames = useMemo<Set<string>>(() => {
    if (!searchQuery.trim()) return new Set(tables.map((t) => t.table_name))
    const q = searchQuery.toLowerCase().trim()
    const matches = new Set<string>()

    tables.forEach((t) => {
      if (t.table_name.toLowerCase().includes(q)) {
        matches.add(t.table_name)
      } else if (t.columns.some((c) => c.name.toLowerCase().includes(q))) {
        matches.add(t.table_name)
      }
    })
    return matches
  }, [tables, searchQuery])

  // Canvas Pan Handlers
  const handleCanvasMouseDown = (e: React.MouseEvent) => {
    // Only pan if clicking on canvas background directly
    if (e.target === containerRef.current || (e.target as HTMLElement).tagName === 'svg') {
      setIsPanning(true)
      setPanStart({ x: e.clientX - pan.x, y: e.clientY - pan.y })
    }
  }

  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (isPanning) {
        setPan({
          x: e.clientX - panStart.x,
          y: e.clientY - panStart.y,
        })
      } else if (draggingTable) {
        setPositions((prev) => ({
          ...prev,
          [draggingTable]: {
            x: Math.max(0, (e.clientX - pan.x) / zoom - dragOffset.x),
            y: Math.max(0, (e.clientY - pan.y) / zoom - dragOffset.y),
          },
        }))
      }
    },
    [isPanning, panStart, draggingTable, pan, zoom, dragOffset]
  )

  const handleMouseUp = () => {
    setIsPanning(false)
    setDraggingTable(null)
  }

  // Wheel Zoom with Focal Point Clamping
  const handleWheel = (e: React.WheelEvent) => {
    e.preventDefault()
    const zoomFactor = e.deltaY < 0 ? 1.1 : 0.9
    setZoom((prev) => Math.min(Math.max(prev * zoomFactor, 0.25), 2.0))
  }

  // Node Drag Initiation
  const handleTableMouseDown = (tableName: string, e: React.MouseEvent) => {
    e.stopPropagation()
    const pos = positions[tableName] || { x: 0, y: 0 }
    setDraggingTable(tableName)
    setDragOffset({
      x: (e.clientX - pan.x) / zoom - pos.x,
      y: (e.clientY - pan.y) / zoom - pos.y,
    })
  }

  // Toolbar Actions
  const handleZoomIn = () => setZoom((z) => Math.min(z + 0.15, 2.0))
  const handleZoomOut = () => setZoom((z) => Math.max(z - 0.15, 0.25))
  const handleResetView = () => {
    setZoom(0.9)
    setPan({ x: 40, y: 40 })
  }
  const handleFitToScreen = () => {
    if (tables.length === 0) return
    setZoom(0.7)
    setPan({ x: 20, y: 20 })
  }

  const toggleFullscreen = () => {
    setIsFullscreen((f) => !f)
  }

  // Compute Bezier Curve for an FK Relation
  const getRelationPath = (rel: ForeignKeyRelation) => {
    const srcPos = positions[rel.sourceTable] || { x: 0, y: 0 }
    const tgtPos = positions[rel.targetTable] || { x: 0, y: 0 }

    const srcTable = tables.find((t) => t.table_name.toLowerCase() === rel.sourceTable.toLowerCase())
    const tgtTable = tables.find((t) => t.table_name.toLowerCase() === rel.targetTable.toLowerCase())

    const srcColIdx = srcTable?.columns.findIndex((c) => c.name.toLowerCase() === rel.sourceCol.toLowerCase()) ?? 0
    const tgtColIdx = tgtTable?.columns.findIndex((c) => c.name.toLowerCase() === rel.targetCol.toLowerCase()) ?? 0

    // Source exit point on right or left edge of card
    const isTargetToTheRight = tgtPos.x >= srcPos.x
    const x1 = isTargetToTheRight ? srcPos.x + TABLE_WIDTH : srcPos.x
    const y1 = srcPos.y + HEADER_HEIGHT + PADDING + srcColIdx * ROW_HEIGHT + ROW_HEIGHT / 2

    const x2 = isTargetToTheRight ? tgtPos.x : tgtPos.x + TABLE_WIDTH
    const y2 = tgtPos.y + HEADER_HEIGHT + PADDING + tgtColIdx * ROW_HEIGHT + ROW_HEIGHT / 2

    const dx = Math.abs(x2 - x1) * 0.5
    const cx1 = isTargetToTheRight ? x1 + dx : x1 - dx
    const cx2 = isTargetToTheRight ? x2 - dx : x2 + dx

    return `M ${x1} ${y1} C ${cx1} ${y1}, ${cx2} ${y2}, ${x2} ${y2}`
  }

  return (
    <div
      ref={containerRef}
      className={`er-diagram-container ${isFullscreen ? 'er-fullscreen' : ''}`}
      onMouseDown={handleCanvasMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onWheel={handleWheel}
      role="region"
      aria-label={`Interactive Visual Entity-Relationship Diagram for ${databaseName}`}
    >
      {/* Interactive Controls Toolbar */}
      <div className="er-toolbar" onClick={(e) => e.stopPropagation()}>
        <div className="er-toolbar-search">
          <span className="er-search-icon" aria-hidden="true">🔍</span>
          <input
            type="text"
            className="er-search-input"
            placeholder="Search tables or columns..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            aria-label="Filter tables by name or column"
          />
          {searchQuery && (
            <button
              type="button"
              className="er-search-clear"
              onClick={() => setSearchQuery('')}
              aria-label="Clear search filter"
            >
              ✕
            </button>
          )}
        </div>

        <div className="er-toolbar-actions">
          <button type="button" className="er-tool-btn" onClick={handleZoomIn} title="Zoom In (+)">
            ➕
          </button>
          <span className="er-zoom-level">{Math.round(zoom * 100)}%</span>
          <button type="button" className="er-tool-btn" onClick={handleZoomOut} title="Zoom Out (-)">
            ➖
          </button>
          <button type="button" className="er-tool-btn" onClick={handleResetView} title="Reset View (100%)">
            🔄
          </button>
          <button type="button" className="er-tool-btn" onClick={handleFitToScreen} title="Fit to Screen">
            🎯
          </button>
          <button
            type="button"
            className="er-tool-btn er-fullscreen-btn"
            onClick={toggleFullscreen}
            title={isFullscreen ? 'Exit Fullscreen' : 'Fullscreen'}
          >
            {isFullscreen ? '✕ Exit' : '⛶ Fullscreen'}
          </button>
        </div>
      </div>

      {/* SVG Canvas for Connectors and Rendered Nodes */}
      <svg className="er-canvas-svg">
        <defs>
          <marker
            id="er-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 1 L 10 5 L 0 9 z" fill="#38bdf8" />
          </marker>
          <marker
            id="er-arrow-active"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M 0 1 L 10 5 L 0 9 z" fill="#fbbf24" />
          </marker>
        </defs>

        <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
          {/* Connector Bezier Curves */}
          {relations.map((rel) => {
            const isHovered =
              hoveredFk === rel.id ||
              hoveredTable === rel.sourceTable ||
              hoveredTable === rel.targetTable

            const isDimmed =
              hoveredTable !== null &&
              hoveredTable !== rel.sourceTable &&
              hoveredTable !== rel.targetTable

            return (
              <g
                key={rel.id}
                className={`er-relation-group ${isHovered ? 'relation-hovered' : ''} ${isDimmed ? 'relation-dimmed' : ''}`}
                onMouseEnter={() => setHoveredFk(rel.id)}
                onMouseLeave={() => setHoveredFk(null)}
              >
                <path
                  d={getRelationPath(rel)}
                  className="er-relation-path-hover-target"
                />
                <path
                  d={getRelationPath(rel)}
                  className={`er-relation-path ${isHovered ? 'path-active' : ''}`}
                  markerEnd={isHovered ? 'url(#er-arrow-active)' : 'url(#er-arrow)'}
                />
              </g>
            )
          })}

          {/* Table Card Nodes */}
          {tables.map((table) => {
            const pos = positions[table.table_name] || { x: 0, y: 0 }
            const isMatch = matchingTableNames.has(table.table_name)
            const isHovered = hoveredTable === table.table_name
            const pks = new Set(table.primary_keys || [])
            const fks = new Set((table.foreign_keys || []).map((f) => f.source_column.toLowerCase()))

            const nodeHeight = HEADER_HEIGHT + PADDING * 2 + table.columns.length * ROW_HEIGHT

            return (
              <foreignObject
                key={table.table_name}
                x={pos.x}
                y={pos.y}
                width={TABLE_WIDTH}
                height={nodeHeight + 8}
                className={`er-table-node ${!isMatch ? 'node-filtered-out' : ''} ${isHovered ? 'node-hovered' : ''}`}
                onMouseEnter={() => setHoveredTable(table.table_name)}
                onMouseLeave={() => setHoveredTable(null)}
              >
                <div
                  className="er-table-card"
                  onMouseDown={(e) => handleTableMouseDown(table.table_name, e)}
                >
                  {/* Table Header */}
                  <div className="er-card-header">
                    <div className="er-card-title-group">
                      <span className="er-card-icon" aria-hidden="true">🗂️</span>
                      <strong className="er-card-title" title={table.table_name}>
                        {table.table_name}
                      </strong>
                    </div>
                    <div className="er-card-actions">
                      {onPeekTable && (
                        <button
                          type="button"
                          className="er-card-action-btn"
                          onClick={(e) => {
                            e.stopPropagation()
                            onPeekTable(table.table_name, table.schema_name)
                          }}
                          title={`Peek first 5 rows of ${table.table_name}`}
                          aria-label={`Peek data for table ${table.table_name}`}
                        >
                          👁️
                        </button>
                      )}
                      {onInspectTable && (
                        <button
                          type="button"
                          className="er-card-action-btn"
                          onClick={(e) => {
                            e.stopPropagation()
                            onInspectTable(table.table_name)
                          }}
                          title={`View AI Data Dictionary for ${table.table_name}`}
                          aria-label={`Inspect data dictionary for table ${table.table_name}`}
                        >
                          📖
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Columns List */}
                  <div className="er-card-columns">
                    {table.columns.map((col) => {
                      const isPk = col.primary_key || pks.has(col.name)
                      const isFk = fks.has(col.name.toLowerCase())
                      return (
                        <div key={col.name} className="er-col-row">
                          <div className="er-col-meta-group">
                            {isPk && <span className="er-badge pk-badge" title="Primary Key">PK</span>}
                            {isFk && <span className="er-badge fk-badge" title="Foreign Key">FK</span>}
                            <span className="er-col-name" title={col.name}>
                              {col.name}
                            </span>
                          </div>
                          <span className="er-col-type" title={col.data_type || 'TEXT'}>
                            {col.data_type || 'TEXT'}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                </div>
              </foreignObject>
            )
          })}
        </g>
      </svg>
    </div>
  )
}
