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
        <radialGradient id="qlogoGlow" cx="50%" cy="42%" r="65%">
          <stop offset="0%" stopColor="#1e293b" />
          <stop offset="55%" stopColor="#0f172a" />
          <stop offset="100%" stopColor="#090d16" />
        </radialGradient>

        <linearGradient id="qlogoRing" x1="100" y1="90" x2="410" y2="410" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#38bdf8" />
          <stop offset="45%" stopColor="#6366f1" />
          <stop offset="100%" stopColor="#a855f7" />
        </linearGradient>

        <linearGradient id="qlogoNeedleLeft" x1="160" y1="310" x2="355" y2="145" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#0284c7" />
          <stop offset="50%" stopColor="#38bdf8" />
          <stop offset="100%" stopColor="#f0f9ff" />
        </linearGradient>

        <linearGradient id="qlogoNeedleRight" x1="240" y1="360" x2="355" y2="145" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#4338ca" />
          <stop offset="50%" stopColor="#6366f1" />
          <stop offset="100%" stopColor="#e0e7ff" />
        </linearGradient>

        <linearGradient id="qlogoTail" x1="260" y1="260" x2="415" y2="415" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#6366f1" />
          <stop offset="100%" stopColor="#38bdf8" />
        </linearGradient>

        <linearGradient id="qlogoTrack" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.8" />
          <stop offset="100%" stopColor="#818cf8" stopOpacity="0.2" />
        </linearGradient>

        <filter id="qlogoFilter" x="-15%" y="-15%" width="130%" height="130%">
          <feGaussianBlur stdDeviation="12" result="blur" />
          <feComposite in="SourceGraphic" in2="blur" operator="over" />
        </filter>
      </defs>

      {/* Squircle Canvas Base */}
      <rect x="24" y="24" width="464" height="464" rx="108" fill="url(#qlogoGlow)" stroke="#1e293b" strokeWidth="5" />

      {/* Data Geometry Orbitals */}
      <circle cx="248" cy="244" r="148" stroke="#334155" strokeWidth="2" strokeDasharray="6 8" strokeOpacity="0.35" />
      <circle cx="248" cy="244" r="98" stroke="#334155" strokeWidth="2" strokeDasharray="4 6" strokeOpacity="0.25" />

      {/* Precision Cardinal Ticks */}
      <line x1="248" y1="62" x2="248" y2="74" stroke="#38bdf8" strokeWidth="3" strokeLinecap="round" />
      <line x1="62" y1="244" x2="74" y2="244" stroke="#38bdf8" strokeWidth="3" strokeLinecap="round" />
      <line x1="248" y1="414" x2="248" y2="426" stroke="#6366f1" strokeWidth="3" strokeLinecap="round" />
      <line x1="414" y1="244" x2="426" y2="244" stroke="#818cf8" strokeWidth="3" strokeLinecap="round" />

      {/* Precision Q Ring */}
      <circle
        cx="248"
        cy="244"
        r="152"
        stroke="url(#qlogoRing)"
        strokeWidth="26"
        fill="none"
        filter="url(#qlogoFilter)"
      />

      {/* Inner Layer Arcs */}
      <path d="M 148 244 A 100 100 0 0 1 332 176" stroke="url(#qlogoTrack)" strokeWidth="6" strokeLinecap="round" />
      <path d="M 168 284 A 100 100 0 0 0 318 314" stroke="url(#qlogoTrack)" strokeWidth="6" strokeLinecap="round" />

      {/* Pilot Compass Delta Arrow */}
      <polygon points="356,136 182,284 242,270" fill="url(#qlogoNeedleLeft)" />
      <polygon points="356,136 242,270 282,346" fill="url(#qlogoNeedleRight)" />
      <line x1="356" y1="136" x2="242" y2="270" stroke="#ffffff" strokeWidth="3" strokeLinecap="round" strokeOpacity="0.95" />

      {/* The Q Tail Fin: Clean, Sharp Blade (NO dot / circle) */}
      <polygon points="268,264 416,412 376,424 238,294" fill="url(#qlogoTail)" filter="url(#qlogoFilter)" />
      <line x1="268" y1="264" x2="416" y2="412" stroke="#ffffff" strokeWidth="2" strokeLinecap="round" strokeOpacity="0.8" />

      {/* Beacon Star */}
      <circle cx="356" cy="136" r="5" fill="#ffffff" />
    </svg>
  )
}
