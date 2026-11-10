/* Small, local SVG set. Unknown icons use a neutral circle; no network required. */
(() => {
  const paths = {
    x:'m6 6 12 12M18 6 6 18', plus:'M12 5v14M5 12h14', minus:'M5 12h14',
    check:'m5 12 4 4L19 6', 'chevron-down':'m6 9 6 6 6-6',
    search:'M21 21l-5-5M19 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0',
    'refresh-cw':'M20 7V3l-4 4M4 17v4l4-4M4 9a8 8 0 0 1 14-5l2 3M20 15a8 8 0 0 1-14 5l-2-3',
    'external-link':'M14 3h7v7M21 3 10 14M10 3H3v18h18v-7',
    crop:'M6 3v15h15M3 6h15v15', 'file-text':'M14 2H4v20h16V8ZM14 2v6h6M8 12h8M8 16h8',
    'calendar-range':'M3 5h18v16H3ZM7 2v6M17 2v6M3 10h18M7 14h4M14 17h3',
    send:'m22 2-7 20-4-9-9-4ZM22 2 11 13',
    copy:'M8 8h13v13H8ZM16 8V3H3v13h5',
    download:'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3',
    'trash-2':'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7',
    library:'M4 3v18M9 3v18M14 3v18M18 3l4 18',
    'settings-2':'M3 6h8M15 6h6M3 18h4M11 18h10M11 3v6M7 15v6',
    'bell-ring':'M5 16V9a7 7 0 0 1 14 0v7l2 2H3ZM10 21h4M1 5l2-3M21 2l2 3',
    'pencil-line':'m4 16 12-12 4 4L8 20H4ZM14 6l4 4M12 21h9',
    'user-round':'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0M4 22v-3a8 8 0 0 1 16 0v3',
    'maximize-2':'M3 9V3h6M15 3h6v6M21 15v6h-6M9 21H3v-6',
    'square':'M4 4h16v16H4Z', server:'M3 3h18v7H3ZM3 14h18v7H3ZM7 6h.01M7 17h.01M11 6h6M11 17h6', info:'M12 11v6M12 7h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
    ban:'m5 5 14 14M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0'
  };
  const aliases = {'rotate-cw':'refresh-cw','send-horizontal':'send','circle-check':'check','file-x-2':'file-text','calendar-clock':'calendar-range','alarm-clock':'calendar-range','history':'refresh-cw','users':'user-round','user':'user-round','list-minus':'minus','circle-minus':'minus'};
  window.renderLocalIcons = () => document.querySelectorAll('i[data-lucide]').forEach(el => {
    const key = aliases[el.dataset.lucide] || el.dataset.lucide;
    const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
    for (const [k,v] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.75','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',width:'18',height:'18'})) svg.setAttribute(k,v);
    const path = document.createElementNS(svg.namespaceURI,'path');
    path.setAttribute('d',paths[key] || 'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0');
    svg.append(path); el.replaceWith(svg);
  });
})();
