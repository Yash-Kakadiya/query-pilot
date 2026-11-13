import React from 'react'

interface QueryPilotLogoProps {
  size?: number
  className?: string
  alt?: string
}

export const QueryPilotLogo: React.FC<QueryPilotLogoProps> = ({
  size = 36,
  className = '',
  alt = 'QueryPilot Logo',
}) => {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 512 512"
      width={size}
      height={size}
      className={`querypilot-brand-logo ${className}`.trim()}
      aria-label={alt}
      role="img"
      fill="none"
    >
      <defs>
        <radialGradient id="qlogoGlow" cx="50%" cy="40%" r="65%">
          <stop offset="0%" stopColor="#1e293b" />
          <stop offset="60%" stopColor="#0f172a" />
          <stop offset="100%" stopColor="#090d16" />
        </radialGradient>

        <linearGradient id="qlogoRing" x1="120" y1="100" x2="400" y2="420" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#38bdf8" />
          <stop offset="45%" stopColor="#6366f1" />
          <stop offset="100%" stopColor="#a855f7" />
        </linearGradient>

        <linearGradient id="qlogoNeedleLeft" x1="160" y1="310" x2="350" y2="160" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#0284c7" />
          <stop offset="50%" stopColor="#38bdf8" />
          <stop offset="100%" stopColor="#e0f2fe" />
        </linearGradient>

        <linearGradient id="qlogoNeedleRight" x1="240" y1="360" x2="350" y2="160" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#4f46e5" />
          <stop offset="60%" stopColor="#6366f1" />
          <stop offset="100%" stopColor="#c7d2fe" />
        </linearGradient>

        <linearGradient id="qlogoTrack" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.8" />
          <stop offset="100%" stopColor="#818cf8" stopOpacity="0.2" />
        </linearGradient>

        <filter id="qlogoFilter" x="-15%" y="-15%" width="130%" height="130%">
          <feGaussianBlur stdDeviation="10" result="blur" />
          <feComposite in="SourceGraphic" in2="blur" operator="over" />
        </filter>
      </defs>

      {/* Squircle Canvas Base */}
      <rect x="24" y="24" width="464" height="464" rx="108" fill="url(#qlogoGlow)" stroke="#1e293b" strokeWidth="6" />

      {/* Data Geometry Orbitals */}
      <circle cx="248" cy="248" r="140" stroke="#334155" strokeWidth="2" strokeDasharray="6 8" strokeOpacity="0.4" />
      <circle cx="248" cy="248" r="95" stroke="#334155" strokeWidth="2" strokeDasharray="4 6" strokeOpacity="0.3" />

      {/* Precision Q Ring */}
      <path
        d="M 248 92
           A 156 156 0 1 0 384 326
           L 410 366
           A 22 22 0 0 1 376 390
           L 346 350
           A 156 156 0 0 0 248 92 Z"
        fill="none"
        stroke="url(#qlogoRing)"
        strokeWidth="26"
        strokeLinecap="round"
        strokeLinejoin="round"
        filter="url(#qlogoFilter)"
      />

      {/* Inner Layer Arcs */}
      <path d="M 152 248 A 96 96 0 0 1 328 184" stroke="url(#qlogoTrack)" strokeWidth="6" strokeLinecap="round" />
      <path d="M 172 288 A 96 96 0 0 0 316 316" stroke="url(#qlogoTrack)" strokeWidth="6" strokeLinecap="round" />

      {/* Pilot Compass Delta Arrow */}
      <polygon points="358,138 184,286 244,272" fill="url(#qlogoNeedleLeft)" />
      <polygon points="358,138 244,272 284,348" fill="url(#qlogoNeedleRight)" />
      <line x1="358" y1="138" x2="244" y2="272" stroke="#ffffff" strokeWidth="3" strokeLinecap="round" />

      {/* Q Tail Accent */}
      <polygon points="252,286 392,416 364,424 234,302" fill="url(#qlogoRing)" opacity="0.95" />
      <circle cx="392" cy="416" r="7" fill="#38bdf8" />

      {/* Beacon Star */}
      <circle cx="358" cy="138" r="5" fill="#ffffff" />
    </svg>
  )
}
