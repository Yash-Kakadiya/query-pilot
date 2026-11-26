import { useState, useEffect } from 'react'
import { DEVELOPER_INFO } from '../constants/developerInfo.ts'
import { openExternalUrl } from '../services/systemBrowser.ts'
import './AboutDeveloperModal.css'

export interface AboutDeveloperModalProps {
  isOpen: boolean
  onClose: () => void
}

export function AboutDeveloperModal({ isOpen, onClose }: AboutDeveloperModalProps) {
  const [gifLoaded, setGifLoaded] = useState<boolean>(false)
  const [gifError, setGifError] = useState<boolean>(false)

  // Escape key handler
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) {
        onClose()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onClose])

  if (!isOpen) return null

  // Resolve GIF URL dynamically with Vite base path (e.g. /app/developer.gif) and cache-buster
  const [cacheBuster] = useState<number>(() => Date.now())
  const rawBaseUrl = import.meta.env.BASE_URL || '/'
  const normalizedBase = rawBaseUrl.endsWith('/') ? rawBaseUrl : `${rawBaseUrl}/`
  const gifUrl = `${normalizedBase}${DEVELOPER_INFO.gif.filename}?t=${cacheBuster}`

  const handleLinkClick = (url: string) => {
    void openExternalUrl(url)
  }

  return (
    <div
      className="about-dev-backdrop"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="about-dev-title"
    >
      <div className="about-dev-dialog" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="about-dev-header">
          <div className="about-dev-header-title">
            <span className="about-dev-header-badge" aria-hidden="true">👨‍💻</span>
            <div>
              <h2 id="about-dev-title" className="about-dev-name">
                About the Developer
              </h2>
              <span className="about-dev-app-tag">QueryPilot Creator Profile</span>
            </div>
          </div>
          <button
            type="button"
            className="about-dev-close-btn"
            onClick={onClose}
            aria-label="Close Developer Profile"
          >
            ✕
          </button>
        </div>

        {/* Modal Body */}
        <div className="about-dev-body">
          {/* Hero Section: Circular Avatar Display or Fallback */}
          <div className="about-dev-hero">
            <div className="about-dev-avatar-frame">
              {!gifError ? (
                <>
                  <img
                    src={gifUrl}
                    alt={DEVELOPER_INFO.gif.alt}
                    className={`about-dev-gif ${gifLoaded ? 'is-loaded' : 'is-loading'}`}
                    onLoad={() => setGifLoaded(true)}
                    onError={() => setGifError(true)}
                  />
                  {!gifLoaded && (
                    <div className="about-dev-media-placeholder">
                      <span className="about-dev-spinner" aria-hidden="true" />
                    </div>
                  )}
                </>
              ) : (
                <div className="about-dev-avatar-circle">
                  <span className="about-dev-avatar-text">
                    {DEVELOPER_INFO.name.slice(0, 2).toUpperCase()}
                  </span>
                </div>
              )}
            </div>

            {gifError && (
              <div className="about-dev-gif-tip">
                <span className="about-dev-tip-icon">💡</span>
                <span>
                  Place your GIF at <code>frontend/public/{DEVELOPER_INFO.gif.filename}</code> to display your custom animation here!
                </span>
              </div>
            )}

            {DEVELOPER_INFO.gif.caption && !gifError && gifLoaded && (
              <p className="about-dev-gif-caption">{DEVELOPER_INFO.gif.caption}</p>
            )}
          </div>

          {/* Profile Identity */}
          <div className="about-dev-profile-meta">
            <h3 className="about-dev-full-name">{DEVELOPER_INFO.name}</h3>
            <p className="about-dev-title-role">{DEVELOPER_INFO.title}</p>
            {DEVELOPER_INFO.location && (
              <span className="about-dev-location">
                📍 {DEVELOPER_INFO.location}
              </span>
            )}
            <p className="about-dev-tagline">"{DEVELOPER_INFO.tagline}"</p>
          </div>

          {/* Bio Section */}
          <div className="about-dev-section">
            <h4 className="about-dev-section-heading">About</h4>
            <div className="about-dev-bio-paragraphs">
              {DEVELOPER_INFO.bio.map((para, idx) => (
                <p key={idx} className="about-dev-bio-p">
                  {para}
                </p>
              ))}
            </div>
          </div>

          {/* Technical Skills & Expertise */}
          <div className="about-dev-section">
            <h4 className="about-dev-section-heading">Core Technologies & Architecture</h4>
            <div className="about-dev-skills-grid">
              {DEVELOPER_INFO.skills.map((skill) => (
                <span key={skill} className="about-dev-skill-pill">
                  {skill}
                </span>
              ))}
            </div>
          </div>

          {/* Connect & Social Profiles */}
          {DEVELOPER_INFO.socialLinks.length > 0 && (
            <div className="about-dev-section">
              <h4 className="about-dev-section-heading">Connect & Social</h4>
              <div className="about-dev-links-grid">
                {DEVELOPER_INFO.socialLinks.map((link) => (
                  <button
                    key={link.id}
                    type="button"
                    className="about-dev-link-card"
                    onClick={() => handleLinkClick(link.url)}
                    title={`Open ${link.label} in browser`}
                  >
                    <span className="about-dev-link-icon" aria-hidden="true">
                      {link.icon}
                    </span>
                    <div className="about-dev-link-text">
                      <span className="about-dev-link-label">{link.label}</span>
                      {link.description && (
                        <span className="about-dev-link-desc">{link.description}</span>
                      )}
                    </div>
                    <span className="about-dev-link-arrow" aria-hidden="true">↗</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="about-dev-footer">
          <span className="about-dev-footer-note">
            Built with craftsmanship for QueryPilot 🚀
          </span>
          <button type="button" className="about-dev-dismiss-btn" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
