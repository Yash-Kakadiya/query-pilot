/**
 * Table Peek Service.
 *
 * Provides rapid, read-only preview of up to 5 rows from an approved table
 * directly from schema navigation or ER diagram nodes.
 */

import { peekTable as apiPeekTable } from './agentClient.ts'
import type { TablePeekResponse } from '../types/api.ts'

export async function fetchTablePeek(
  profileId: string,
  tableName: string,
  limit: number = 5
): Promise<TablePeekResponse> {
  return apiPeekTable(profileId, tableName, limit)
}
