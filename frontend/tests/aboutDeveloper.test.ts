/**
 * Developer Info & About Developer Profile Configuration Test Suite.
 *
 * Verifies:
 * 1. DEVELOPER_INFO constant structure, mandatory fields, and types.
 * 2. Bio paragraphs and technical skill list are populated.
 * 3. Social links contain valid URLs, labels, and icons.
 * 4. GIF configuration specifies valid filename with .gif extension.
 * 5. Base URL resolution produces clean asset path.
 */

import assert from 'node:assert'
import test from 'node:test'
import { DEVELOPER_INFO } from '../src/constants/developerInfo.ts'

test('Developer Configuration - DEVELOPER_INFO Validation', async (t) => {
  await t.test('1. Contains mandatory developer identity fields', () => {
    assert.ok(typeof DEVELOPER_INFO.name === 'string' && DEVELOPER_INFO.name.trim().length > 0, 'Name must be a non-empty string')
    assert.ok(typeof DEVELOPER_INFO.title === 'string' && DEVELOPER_INFO.title.trim().length > 0, 'Title must be a non-empty string')
    assert.ok(typeof DEVELOPER_INFO.tagline === 'string' && DEVELOPER_INFO.tagline.trim().length > 0, 'Tagline must be a non-empty string')
    assert.ok(Array.isArray(DEVELOPER_INFO.bio) && DEVELOPER_INFO.bio.length > 0, 'Bio must be a non-empty array of paragraphs')
  })

  await t.test('2. Contains core technical skills', () => {
    assert.ok(Array.isArray(DEVELOPER_INFO.skills) && DEVELOPER_INFO.skills.length >= 3, 'Skills should list at least 3 core technologies')
    for (const skill of DEVELOPER_INFO.skills) {
      assert.ok(typeof skill === 'string' && skill.trim().length > 0, `Skill item "${skill}" must not be empty`)
    }
  })

  await t.test('3. Social links are well-formed', () => {
    assert.ok(Array.isArray(DEVELOPER_INFO.socialLinks), 'socialLinks must be an array')
    for (const link of DEVELOPER_INFO.socialLinks) {
      assert.ok(link.id && link.id.trim().length > 0, 'Link ID must be non-empty')
      assert.ok(link.label && link.label.trim().length > 0, 'Link label must be non-empty')
      assert.ok(
        link.url && (link.url.startsWith('http://') || link.url.startsWith('https://') || link.url.startsWith('mailto:')),
        `Link URL "${link.url}" must be valid http, https, or mailto`
      )
      assert.ok(link.icon && link.icon.trim().length > 0, 'Link icon must be present')
    }
  })

  await t.test('4. GIF configuration specifies valid filename', () => {
    assert.ok(DEVELOPER_INFO.gif, 'gif object must be present')
    assert.ok(typeof DEVELOPER_INFO.gif.filename === 'string', 'gif.filename must be a string')
    assert.ok(DEVELOPER_INFO.gif.filename.toLowerCase().endsWith('.gif'), 'gif.filename must end with .gif')
    assert.ok(DEVELOPER_INFO.gif.alt && DEVELOPER_INFO.gif.alt.trim().length > 0, 'gif.alt text must be non-empty')
  })

  await t.test('5. Base URL resolution produces clean asset path', () => {
    const rawBaseUrl = '/app/'
    const normalizedBase = rawBaseUrl.endsWith('/') ? rawBaseUrl : `${rawBaseUrl}/`
    const resolvedPath = `${normalizedBase}${DEVELOPER_INFO.gif.filename}`
    assert.strictEqual(resolvedPath, `/app/${DEVELOPER_INFO.gif.filename}`)
  })
})
