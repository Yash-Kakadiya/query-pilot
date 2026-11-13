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
    <img
      src="/logo.svg"
      width={size}
      height={size}
      alt={alt}
      className={`querypilot-brand-logo ${className}`.trim()}
      style={{
        display: 'inline-block',
        verticalAlign: 'middle',
        borderRadius: `${Math.round(size * 0.22)}px`,
      }}
    />
  )
}
