'use strict';
(() => {
  const statusEl = document.getElementById('qr-status');
  const btnStart = document.getElementById('qr-camera');
  const btnStop = document.getElementById('qr-stop');
  const placeholder = document.getElementById('camera-placeholder');
  const videoEl = document.getElementById('qr-video');
  const overlayEl = document.getElementById('qr-overlay');

  let html5QrScanner = null;
  let nativeStream = null;
  let isScanning = false;

  const setStatus = (msg, isError = false) => {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.className = 'alert ' + (isError ? 'alert-danger' : 'alert-info') + ' py-2 px-3 small mb-0 text-center';
  };

  const openEquipment = (rawCode) => {
    let code = (rawCode || '').trim();
    if (!code) return;

    // Check if it's a direct UUID
    const uuidRegex = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    if (uuidRegex.test(code)) {
      stopScanning();
      setStatus('Equipamento identificado! Redirecionando...');
      window.location.assign('/equipamentos/qr/' + encodeURIComponent(code) + '/');
      return;
    }

    // Try parsing as URL
    try {
      const url = new URL(code, window.location.origin);
      const match = url.pathname.match(/\/equipamentos\/qr\/([0-9a-f-]{36})\/?/i);
      if (match && match[1]) {
        stopScanning();
        setStatus('Equipamento identificado! Redirecionando...');
        window.location.assign('/equipamentos/qr/' + encodeURIComponent(match[1]) + '/');
        return;
      }
    } catch (e) {
      // not a valid URL
    }

    setStatus('QR Code não reconhecido como equipamento Vertyx. Verifique se a etiqueta é deste sistema.', true);
  };

  const stopScanning = () => {
    isScanning = false;
    if (html5QrScanner) {
      try {
        html5QrScanner.stop().catch(() => {}).then(() => {
          html5QrScanner.clear();
        });
      } catch (e) {}
      html5QrScanner = null;
    }

    if (nativeStream) {
      nativeStream.getTracks().forEach(track => track.stop());
      nativeStream = null;
    }
    if (videoEl) {
      videoEl.srcObject = null;
      videoEl.style.display = 'none';
    }
    if (overlayEl) overlayEl.style.setProperty('display', 'none', 'important');
    if (placeholder) placeholder.style.display = 'block';
    if (btnStart) btnStart.style.display = 'inline-block';
    if (btnStop) btnStop.style.display = 'none';
  };

  const startScanning = async () => {
    stopScanning();

    // Strategy 1: Html5Qrcode if available (best cross-platform mobile compatibility including iOS)
    if (typeof Html5Qrcode !== 'undefined') {
      try {
        if (placeholder) placeholder.style.display = 'none';
        btnStart.style.display = 'none';
        btnStop.style.display = 'inline-block';
        setStatus('Iniciando câmera... Aponte para o QR Code.');

        html5QrScanner = new Html5Qrcode('qr-reader');
        const config = {
          fps: 10,
          qrbox: { width: 220, height: 220 },
          aspectRatio: 1.0
        };

        await html5QrScanner.start(
          { facingMode: 'environment' },
          config,
          (decodedText) => {
            openEquipment(decodedText);
          },
          () => {
            // frame scanning, ignore individual failed frames
          }
        );

        isScanning = true;
        setStatus('Câmera ativa. Posicione o QR Code no centro da tela.');
        return;
      } catch (err) {
        console.warn('Falha no Html5Qrcode, tentando fallback nativo:', err);
        stopScanning();
      }
    }

    // Strategy 2: Native BarcodeDetector fallback
    if ('BarcodeDetector' in window && navigator.mediaDevices?.getUserMedia) {
      try {
        const detector = new BarcodeDetector({ formats: ['qr_code'] });
        nativeStream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: 'environment' },
          audio: false
        });

        if (placeholder) placeholder.style.display = 'none';
        videoEl.style.display = 'block';
        if (overlayEl) overlayEl.style.setProperty('display', 'flex', 'important');
        videoEl.srcObject = nativeStream;
        await videoEl.play();

        btnStart.style.display = 'none';
        btnStop.style.display = 'inline-block';
        isScanning = true;
        setStatus('Aponte para o QR Code do equipamento.');

        const scanFrame = async () => {
          if (!isScanning) return;
          try {
            const codes = await detector.detect(videoEl);
            if (codes && codes.length > 0) {
              openEquipment(codes[0].rawValue);
              return;
            }
          } catch (e) {
            // ignore scan frame error
          }
          if (isScanning) requestAnimationFrame(scanFrame);
        };
        requestAnimationFrame(scanFrame);
        return;
      } catch (err) {
        console.warn('Erro no fallback nativo:', err);
      }
    }

    setStatus('Permissão de câmera não concedida ou navegador sem suporte. Utilize a busca manual abaixo.', true);
    stopScanning();
  };

  // Event Listeners
  if (btnStart) btnStart.addEventListener('click', startScanning);
  if (btnStop) {
    btnStop.addEventListener('click', () => {
      stopScanning();
      setStatus('Câmera pausada. Você pode reativar ou digitar o código.');
    });
  }

  const manualForm = document.getElementById('qr-manual');
  if (manualForm) {
    manualForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const codeInput = document.getElementById('qr-code');
      if (codeInput) openEquipment(codeInput.value);
    });
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopScanning();
  });
  window.addEventListener('pagehide', stopScanning);
})();
