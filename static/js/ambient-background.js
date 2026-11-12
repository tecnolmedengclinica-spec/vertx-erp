/**
 * VERTYX Ambient Background Animation
 * Canvas-based lightweight ambient particle mesh & glowing aurora.
 * Zero dependencies, high performance (60fps), adaptive to Dark & Light themes.
 */

'use strict';

(() => {
  const canvas = document.getElementById('vx-ambient-canvas');
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  let animationFrameId = null;
  let width = 0;
  let height = 0;
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  // Particle configuration
  const PARTICLE_COUNT_DESKTOP = 26;
  const PARTICLE_COUNT_MOBILE = 14;
  const CONNECT_DISTANCE = 140;

  let particles = [];
  let isDarkMode = document.documentElement.getAttribute('data-theme') !== 'light';

  const getColors = () => {
    return isDarkMode
      ? [
          { r: 59, g: 130, b: 246 },  // Brand Blue (#3b82f6)
          { r: 56, g: 189, b: 248 },  // Sky Cyan (#38bdf8)
          { r: 16, g: 185, b: 129 },  // Emerald (#10b981)
          { r: 99, g: 102, b: 241 },  // Indigo (#6366f1)
          { r: 168, g: 85, b: 247 }   // Violet (#a855f7)
        ]
      : [
          { r: 37, g: 99, b: 235 },   // Royal Blue
          { r: 2, g: 132, b: 199 },   // Deep Cyan
          { r: 5, g: 150, b: 105 },   // Deep Emerald
          { r: 79, g: 70, b: 229 },   // Deep Indigo
          { r: 147, g: 51, b: 234 }   // Purple
        ];
  };

  class Particle {
    constructor() {
      this.reset(true);
    }

    reset(initial = false) {
      this.x = initial ? Math.random() * width : (Math.random() < 0.5 ? -10 : width + 10);
      this.y = Math.random() * height;
      this.radius = Math.random() * 2.2 + 1.2;

      const speed = Math.random() * 0.45 + 0.2;
      const angle = Math.random() * Math.PI * 2;
      this.vx = Math.cos(angle) * speed;
      this.vy = Math.sin(angle) * speed;

      const colors = getColors();
      this.color = colors[Math.floor(Math.random() * colors.length)];
      this.alpha = Math.random() * 0.4 + 0.2;
      this.pulseSpeed = Math.random() * 0.015 + 0.005;
      this.pulseVal = Math.random() * Math.PI;
    }

    update() {
      this.x += this.vx;
      this.y += this.vy;

      this.pulseVal += this.pulseSpeed;
      this.currentAlpha = this.alpha * (0.7 + 0.3 * Math.sin(this.pulseVal));

      if (this.x < -20 || this.x > width + 20 || this.y < -20 || this.y > height + 20) {
        this.reset();
      }
    }

    draw() {
      // Soft radial glow
      const glowRadius = this.radius * 7;
      const gradient = ctx.createRadialGradient(
        this.x, this.y, 0,
        this.x, this.y, glowRadius
      );
      const c = this.color;
      gradient.addColorStop(0, `rgba(${c.r}, ${c.g}, ${c.b}, ${this.currentAlpha * (isDarkMode ? 0.35 : 0.2)})`);
      gradient.addColorStop(1, `rgba(${c.r}, ${c.g}, ${c.b}, 0)`);

      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.arc(this.x, this.y, glowRadius, 0, Math.PI * 2);
      ctx.fill();

      // Sharp central core
      ctx.fillStyle = `rgba(${c.r}, ${c.g}, ${c.b}, ${this.currentAlpha * (isDarkMode ? 0.9 : 0.75)})`;
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // Large subtle ambient background orbs
  const drawAmbientGradients = () => {
    const orb1X = width * 0.25;
    const orb1Y = height * 0.3;
    const orb1Radius = Math.min(width, height) * 0.55;

    const orb2X = width * 0.8;
    const orb2Y = height * 0.7;
    const orb2Radius = Math.min(width, height) * 0.6;

    // Orb 1 (Brand Blue / Cyan)
    const g1 = ctx.createRadialGradient(orb1X, orb1Y, 0, orb1X, orb1Y, orb1Radius);
    if (isDarkMode) {
      g1.addColorStop(0, 'rgba(37, 99, 235, 0.12)');
      g1.addColorStop(0.5, 'rgba(6, 182, 212, 0.04)');
      g1.addColorStop(1, 'rgba(11, 17, 29, 0)');
    } else {
      g1.addColorStop(0, 'rgba(191, 219, 254, 0.45)');
      g1.addColorStop(0.5, 'rgba(186, 230, 253, 0.2)');
      g1.addColorStop(1, 'rgba(248, 250, 252, 0)');
    }
    ctx.fillStyle = g1;
    ctx.fillRect(0, 0, width, height);

    // Orb 2 (Emerald / Indigo)
    const g2 = ctx.createRadialGradient(orb2X, orb2Y, 0, orb2X, orb2Y, orb2Radius);
    if (isDarkMode) {
      g2.addColorStop(0, 'rgba(16, 185, 129, 0.08)');
      g2.addColorStop(0.6, 'rgba(99, 102, 241, 0.03)');
      g2.addColorStop(1, 'rgba(11, 17, 29, 0)');
    } else {
      g2.addColorStop(0, 'rgba(167, 243, 208, 0.35)');
      g2.addColorStop(0.6, 'rgba(224, 231, 255, 0.2)');
      g2.addColorStop(1, 'rgba(248, 250, 252, 0)');
    }
    ctx.fillStyle = g2;
    ctx.fillRect(0, 0, width, height);
  };

  const drawConnections = () => {
    const len = particles.length;
    for (let i = 0; i < len; i++) {
      for (let j = i + 1; j < len; j++) {
        const dx = particles[i].x - particles[j].x;
        const dy = particles[i].y - particles[j].y;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist < CONNECT_DISTANCE) {
          const proximity = 1 - dist / CONNECT_DISTANCE;
          const alpha = proximity * 0.15 * (isDarkMode ? 1 : 0.8);
          const c1 = particles[i].color;

          ctx.strokeStyle = `rgba(${c1.r}, ${c1.g}, ${c1.b}, ${alpha})`;
          ctx.lineWidth = 0.8;
          ctx.beginPath();
          ctx.moveTo(particles[i].x, particles[i].y);
          ctx.lineTo(particles[j].x, particles[j].y);
          ctx.stroke();
        }
      }
    }
  };

  const resize = () => {
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.scale(dpr, dpr);

    const count = width < 768 ? PARTICLE_COUNT_MOBILE : PARTICLE_COUNT_DESKTOP;
    particles = [];
    for (let i = 0; i < count; i++) {
      particles.push(new Particle());
    }
  };

  const animate = () => {
    ctx.clearRect(0, 0, width, height);

    drawAmbientGradients();

    particles.forEach(p => {
      p.update();
      p.draw();
    });

    drawConnections();

    animationFrameId = requestAnimationFrame(animate);
  };

  const updateTheme = () => {
    isDarkMode = document.documentElement.getAttribute('data-theme') !== 'light';
    particles.forEach(p => {
      const colors = getColors();
      p.color = colors[Math.floor(Math.random() * colors.length)];
    });
  };

  // Visibility and resize handlers
  window.addEventListener('resize', () => {
    cancelAnimationFrame(animationFrameId);
    resize();
    animate();
  });

  window.addEventListener('vertyxThemeChanged', updateTheme);

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      cancelAnimationFrame(animationFrameId);
    } else {
      animate();
    }
  });

  // Respect prefers-reduced-motion
  const mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  if (!mediaQuery.matches) {
    resize();
    animate();
  } else {
    resize();
    drawAmbientGradients();
  }
})();
