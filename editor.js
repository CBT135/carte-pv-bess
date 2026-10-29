/* Repositionnement manuel des installations + enregistrement protégé par mot de passe.
 *
 * Les corrections sont stockées dans data/positions.json du dépôt GitHub.
 * L'écriture passe par l'API GitHub avec un jeton (fine-grained, « Contents: read and write »
 * sur ce seul dépôt). Ce jeton n'est jamais publié en clair : il est chiffré (AES-GCM,
 * clé dérivée du mot de passe par PBKDF2-SHA256) dans data/editor.json. Sans le bon mot de
 * passe, la page ne peut pas le déchiffrer, donc pas écrire.
 */
"use strict";
const EDIT = (() => {
  const host = location.hostname;
  const OWNER = host.endsWith(".github.io") ? host.split(".")[0] : "CBT135";
  const REPO = host.endsWith(".github.io") ? (location.pathname.split("/")[1] || "carte-pv-bess") : "carte-pv-bess";
  const API = `https://api.github.com/repos/${OWNER}/${REPO}/contents/`;
  const ITER = 250000;
  const keyOf = p => p.eic || `${p.nom}|${p.insee}`;

  let POS = {};            // corrections enregistrées
  let pending = {};        // corrections en cours (null = retour à la position d'origine)
  let pendingExpl = {};    // exploitants saisis (clé exploitants.csv → libellé, null = retirer la saisie)
  let EXPL = {};           // data/exploitants.csv (clés exactes, en majuscules)
  const csvKey = p => p.eic || p.nom;
  const upk = s => String(s || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toUpperCase().trim();
  let on = false, drag = null;

  // ------------------------------------------------------------ utilitaires
  const b64e = buf => btoa(String.fromCharCode(...new Uint8Array(buf)));
  const b64d = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const utf8b64 = s => btoa(unescape(encodeURIComponent(s)));
  const b64utf8 = s => decodeURIComponent(escape(atob(s.replace(/\n/g, ""))));
  async function deriveKey(pw, salt){
    const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(pw), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({name:"PBKDF2", salt, iterations:ITER, hash:"SHA-256"}, base,
      {name:"AES-GCM", length:256}, false, ["encrypt","decrypt"]);
  }
  async function encryptToken(token, pw){
    const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({name:"AES-GCM", iv}, await deriveKey(pw, salt), new TextEncoder().encode(token));
    return {v:1, kdf:"PBKDF2-SHA256", iter:ITER, salt:b64e(salt), iv:b64e(iv), ct:b64e(ct)};
  }
  async function decryptToken(blob, pw){
    try{
      const pt = await crypto.subtle.decrypt({name:"AES-GCM", iv:b64d(blob.iv)},
        await deriveKey(pw, b64d(blob.salt)), b64d(blob.ct));
      return new TextDecoder().decode(pt);
    }catch(e){ throw new Error("Mot de passe incorrect."); }
  }
  async function gh(path, token, opts={}){
    const r = await fetch(API + path + (opts.method ? "" : `?ref=main&t=${Date.now()}`), {
      ...opts, headers:{Accept:"application/vnd.github+json", Authorization:`Bearer ${token}`,
        "X-GitHub-Api-Version":"2022-11-28", ...(opts.headers||{})}});
    if (r.status === 404 && !opts.method) return null;
    if (!r.ok){
      const j = await r.json().catch(()=>({}));
      if (r.status === 401) throw new Error("Jeton GitHub refusé (expiré ou révoqué) : collez un nouveau jeton ci-dessus puis réessayez.");
      if (r.status === 403) throw new Error("Jeton GitHub sans droit d'écriture sur ce dépôt.");
      throw new Error(`GitHub ${r.status} : ${j.message || r.statusText}`);
    }
    return r.json();
  }
  async function putFile(path, text, sha, token, message){
    return gh(path, token, {method:"PUT", body:JSON.stringify({message, content:utf8b64(text), sha:sha||undefined, branch:"main"})});
  }

  // ------------------------------------------------------------ chargement
  function parseCsv(text){
    const out = {};
    String(text || "").split(/\r?\n/).forEach(l => {
      if (!l.trim() || l.startsWith("#")) return;
      const i = l.indexOf(";"); if (i < 1) return;
      const k = l.slice(0, i).trim(), v = l.slice(i + 1).split(";")[0].trim();
      if (!v || k.toLowerCase() === "cle" || k.toLowerCase().startsWith("re:")) return;
      out[upk(k)] = v;
    });
    return out;
  }
  async function load(){
    try{
      const r = await fetch("data/positions.json", {cache:"no-cache"});
      POS = r.ok ? await r.json() : {};
    }catch(e){ POS = {}; }
    try{
      const r = await fetch("data/exploitants.csv", {cache:"no-cache"});
      EXPL = r.ok ? parseCsv(await r.text()) : {};
    }catch(e){ EXPL = {}; }
  }
  function applyTo(data){
    data.forEach(p => {
      const e = EXPL[upk(p.eic)] || EXPL[upk(p.nom)];
      if (e){ p._origExpl = p._origExpl || [p.expl, p.expl_src]; p.expl = e; p.expl_src = "fichier"; }
      const c = POS[keyOf(p)];
      if (c){ p._orig = p._orig || [p.lat, p.lon, p.loc_src]; p.lat = c.lat; p.lon = c.lon; p.loc_src = "manuel"; p.pos_date = c.date; }
    });
  }

  // ------------------------------------------------------------ interface
  const css = `
  .ed-btn{background:#fff;border:0;border-radius:8px;box-shadow:0 2px 10px #0f172a2e;font:600 13px Inter,system-ui,sans-serif;
    padding:8px 12px;cursor:pointer;color:#18212b}
  .ed-btn.on{background:#18212b;color:#fff}
  #edBar{position:absolute;left:50%;bottom:24px;transform:translateX(-50%);z-index:1500;display:none;align-items:center;gap:10px;
    background:#18212b;color:#fff;border-radius:12px;padding:10px 12px 10px 16px;font:13px Inter,system-ui,sans-serif;box-shadow:0 8px 28px #0f172a55;max-width:calc(100% - 24px)}
  #edBar button{font:600 13px Inter,system-ui,sans-serif;border-radius:8px;padding:7px 12px;cursor:pointer;border:1px solid #ffffff44;background:transparent;color:#fff}
  #edBar button.primary{background:#f2a20c;border-color:#f2a20c;color:#18212b}
  #edBar button:disabled{opacity:.4;cursor:default}
  #edModal{position:fixed;inset:0;background:#0f172a66;z-index:3000;display:none;align-items:center;justify-content:center;padding:16px}
  #edModal .box{background:#fff;border-radius:14px;padding:20px;width:100%;max-width:400px;font:14px/1.45 Inter,system-ui,sans-serif;color:#18212b}
  #edModal h2{font-size:16px;margin:0 0 10px}
  #edModal label{display:block;font-size:12px;font-weight:600;color:#5d6b7a;margin:12px 0 4px}
  #edModal input{width:100%;font:inherit;padding:9px 10px;border:1px solid #e2e6eb;border-radius:8px}
  #edModal .help{font-size:12px;color:#5d6b7a;margin-top:6px}
  #edModal .row{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
  #edModal .row button{font:600 13px Inter,system-ui,sans-serif;border-radius:8px;padding:9px 14px;cursor:pointer;border:1px solid #e2e6eb;background:#fff}
  #edModal .row button.primary{background:#18212b;color:#fff;border-color:#18212b}
  #edMsg{margin-top:10px;font-size:13px}
  #edMsg.err{color:#b42318} #edMsg.ok{color:#1a7f37}
  .leaflet-container.ed-on{cursor:default}
  .leaflet-container.ed-drag{cursor:grabbing!important}`;
  document.head.insertAdjacentHTML("beforeend", `<style>${css}</style>`);
  document.body.insertAdjacentHTML("beforeend", `
    <div id="edBar"><span id="edTxt"></span>
      <button type="button" id="edUndo">Annuler</button>
      <button type="button" id="edSave" class="primary">Enregistrer…</button></div>
    <div id="edModal"><div class="box">
      <h2>Enregistrer les modifications</h2>
      <div id="edSetup" style="display:none">
        <div class="help">Première utilisation : collez un jeton GitHub <i>fine-grained</i> limité au dépôt
          <b>${OWNER}/${REPO}</b> avec la permission <b>Contents : Read and write</b>. Il sera chiffré avec le mot de passe
          et stocké dans <code>data/editor.json</code> ; il ne sera plus demandé ensuite.</div>
        <label for="edTok">Jeton GitHub</label><input id="edTok" type="password" autocomplete="off" placeholder="github_pat_…">
      </div>
      <div id="edReplace" style="display:none;margin-top:4px"><a href="#" id="edReplaceLink" style="font-size:12px;color:#5d6b7a">Jeton expiré ? Remplacer le jeton GitHub</a></div>
      <label for="edPw">Mot de passe</label><input id="edPw" type="password" autocomplete="current-password">
      <div id="edMsg"></div>
      <div class="row"><button type="button" id="edCancel">Fermer</button><button type="button" id="edGo" class="primary">Enregistrer</button></div>
    </div></div>`);

  const ctl = L.control({position:"topleft"});
  ctl.onAdd = () => {
    const b = L.DomUtil.create("button", "ed-btn");
    b.type = "button"; b.textContent = "✎ Éditer"; b.id = "edToggle";
    b.title = "Déplacer les installations à la souris et saisir leur exploitant";
    L.DomEvent.disableClickPropagation(b);
    b.onclick = () => toggle();
    return b;
  };
  ctl.addTo(map);

  const n = () => Object.keys(pending).length + Object.keys(pendingExpl).length;
  function refreshBar(){
    $("edBar").style.display = on ? "flex" : "none";
    $("edTxt").textContent = n() ? `${n()} modification(s) non enregistrée(s)` :
      "Glissez un point pour le déplacer · cliquez-le pour saisir son exploitant · clic droit = position d'origine";
    $("edUndo").disabled = $("edSave").disabled = !n();
  }
  function toggle(force){
    if (on && n() && force === undefined && !confirm("Quitter le mode édition ? Les modifications non enregistrées restent en attente.")) return;
    if (on) closeExpl();
    on = force !== undefined ? force : !on;
    $("edToggle").classList.toggle("on", on);
    map.getContainer().classList.toggle("ed-on", on);
    refreshBar();
  }
  function styleMoved(m){
    const moved = m._p.loc_src === "manuel" || pending[keyOf(m._p)] !== undefined
      || m._p.expl_src === "fichier" || pendingExpl[csvKey(m._p)] !== undefined;
    m.setStyle(moved ? {color:"#18212b", weight:1.6, dashArray:null} : {color:"#ffffff", weight:.8});
    m._moved = moved;
  }

  // ------------------------------------------------------------ glisser-déposer
  function attach(ms){
    ms.forEach(m => {
      styleMoved(m);
      m.on("mousedown", e => {
        if (!on) return;
        L.DomEvent.stop(e);
        drag = m; dragStart = e.containerPoint; dragMoved = false; m.closeTooltip();
        map.dragging.disable();
      });
      m.on("click", e => { if (on) L.DomEvent.stop(e); });
      m.on("contextmenu", e => {
        if (!on) return;
        L.DomEvent.stop(e);
        const p = m._p, k = keyOf(p);
        if (!(POS[k] || pending[k])) return;
        if (!confirm(`Revenir à la position d'origine pour « ${p.nom} » ?`)) return;
        if (pending[k] && !POS[k]) { delete pending[k]; } else { pending[k] = null; }
        if (p._orig){ [p.lat, p.lon] = p._orig; p.loc_src = p._orig[2]; m.setLatLng([p.lat, p.lon]); }
        styleMoved(m); refreshBar();
      });
      m.off("mouseout"); m.on("mouseout", () => { if (!m._moved) m.setStyle({color:"#ffffff", weight:.8}); else styleMoved(m); });
    });
  }
  let dragStart = null, dragMoved = false;
  map.on("mousemove", e => {
    if (!drag) return;
    if (!dragMoved && dragStart && e.containerPoint.distanceTo(dragStart) < 4) return;
    if (!dragMoved){ dragMoved = true; map.getContainer().classList.add("ed-drag"); closeExpl(); }
    drag.setLatLng(e.latlng);
  });
  const endDrag = () => {
    if (!drag) return;
    const m = drag, p = m._p, ll = m.getLatLng();
    drag = null;
    map.dragging.enable();
    map.getContainer().classList.remove("ed-drag");
    if (!dragMoved){ openExpl(m); return; }
    if (!p._orig) p._orig = [p.lat, p.lon, p.loc_src];
    p.lat = +ll.lat.toFixed(6); p.lon = +ll.lng.toFixed(6); p.loc_src = "manuel";
    pending[keyOf(p)] = {lat:p.lat, lon:p.lon, nom:p.nom, insee:p.insee};
    styleMoved(m); refreshBar();
  };
  map.on("mouseup", endDrag);
  document.addEventListener("mouseup", endDrag);
  window.addEventListener("beforeunload", e => { if (n()) { e.preventDefault(); e.returnValue = ""; } });

  $("edUndo").onclick = () => {
    if (!confirm("Annuler toutes les modifications non enregistrées ?")) return;
    markers.forEach(m => {
      const p = m._p, k = keyOf(p);
      if (pending[k] === undefined) return;
      const c = POS[k];
      if (c){ p.lat = c.lat; p.lon = c.lon; p.loc_src = "manuel"; }
      else if (p._orig){ [p.lat, p.lon] = p._orig; p.loc_src = p._orig[2]; }
      m.setLatLng([p.lat, p.lon]);
    });
    markers.forEach(m => {
      const p = m._p, k = csvKey(p);
      if (pendingExpl[k] === undefined) return;
      const saved = EXPL[upk(p.eic)] || EXPL[upk(p.nom)];
      if (saved){ p.expl = saved; p.expl_src = "fichier"; }
      else if (p._origExpl){ [p.expl, p.expl_src] = p._origExpl; }
    });
    pending = {}; pendingExpl = {}; closeExpl(); markers.forEach(styleMoved); refreshBar();
  };

  // ------------------------------------------------------------ saisie de l'exploitant
  document.body.insertAdjacentHTML("beforeend", `
    <div id="edExpl"><div class="hd"><b id="edExNom"></b><button type="button" id="edExX" aria-label="Fermer">×</button></div>
      <div id="edExInfo"></div>
      <label for="edExIn">Exploitant</label>
      <input id="edExIn" list="explList" autocomplete="off" placeholder="Nom du groupe ou de la société">
      <div class="row"><button type="button" id="edExDel">Retirer la saisie</button><button type="button" id="edExOk" class="primary">Valider</button></div>
    </div>`);
  document.head.insertAdjacentHTML("beforeend", `<style>
    #edExpl{position:absolute;right:16px;bottom:84px;z-index:1600;display:none;width:320px;max-width:calc(100% - 32px);background:#fff;border-radius:12px;
      box-shadow:0 8px 28px #0f172a40;padding:12px 14px;font:13px/1.4 Inter,system-ui,sans-serif;color:#18212b}
    #edExpl .hd{display:flex;gap:8px;align-items:flex-start;justify-content:space-between}
    #edExpl .hd b{font-size:13.5px}
    #edExpl .hd button{border:0;background:none;font-size:20px;line-height:1;cursor:pointer;color:#5d6b7a;padding:0 2px}
    #edExInfo{color:#5d6b7a;font-size:12px;margin:4px 0 8px}
    #edExpl label{display:block;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#5d6b7a;margin-bottom:4px}
    #edExpl input{width:100%;font:inherit;font-size:14px;padding:8px 10px;border:1px solid #e2e6eb;border-radius:8px}
    #edExpl .row{display:flex;gap:8px;justify-content:flex-end;margin-top:10px}
    #edExpl .row button{font:600 13px Inter,system-ui,sans-serif;border-radius:8px;padding:7px 12px;cursor:pointer;border:1px solid #e2e6eb;background:#fff;color:#18212b}
    #edExpl .row button.primary{background:#18212b;color:#fff;border-color:#18212b}
    #edExpl .row button:disabled{opacity:.4;cursor:default}
  </style>`);
  let exM = null;
  function openExpl(m){
    exM = m; const p = m._p;
    $("edExNom").textContent = p.nom;
    const src = {fichier:"saisi manuellement", sirene:"SIRENE", sirene_commune:"SIRENE, probable", osm:"OpenStreetMap", wikidata:"Wikidata", cre:"appel d'offres CRE", site:"autres tranches du site", motif:"codification du nom", nom:"déduit du nom"}[p.expl_src];
    $("edExInfo").textContent = `${(p.mw||0).toLocaleString("fr-FR")} MW · ${p.commune || ""}` +
      (p.expl ? ` · actuel : ${p.expl}${src ? " (" + src + ")" : ""}` : " · exploitant non identifié");
    $("edExIn").value = p.expl_src === "fichier" ? p.expl : "";
    $("edExIn").placeholder = p.expl && p.expl_src !== "fichier" ? p.expl : "Nom du groupe ou de la société";
    $("edExDel").disabled = !(p.expl_src === "fichier");
    $("edExpl").style.display = "block";
    setTimeout(() => $("edExIn").focus(), 30);
  }
  function closeExpl(){ const b = $("edExpl"); if (b) b.style.display = "none"; exM = null; }
  $("edExX").onclick = closeExpl;
  $("edExIn").addEventListener("keydown", e => { if (e.key === "Enter") $("edExOk").click(); if (e.key === "Escape") closeExpl(); });
  $("edExOk").onclick = () => {
    if (!exM) return;
    const p = exM._p, v = $("edExIn").value.trim();
    if (!v) { $("edExIn").focus(); return; }
    if (!p._origExpl) p._origExpl = [p.expl, p.expl_src];
    p.expl = v; p.expl_src = "fichier";
    pendingExpl[csvKey(p)] = v;
    styleMoved(exM); refreshBar(); closeExpl();
  };
  $("edExDel").onclick = () => {
    if (!exM) return;
    const p = exM._p, k = csvKey(p);
    const saved = EXPL[upk(p.eic)] || EXPL[upk(p.nom)];
    if (saved) pendingExpl[k] = null; else delete pendingExpl[k];
    if (p._origExpl){ [p.expl, p.expl_src] = p._origExpl; } else { p.expl = null; p.expl_src = null; }
    styleMoved(exM); refreshBar(); closeExpl();
  };

  // ------------------------------------------------------------ enregistrement
  let editorBlob;
  $("edSave").onclick = async () => {
    $("edMsg").textContent = ""; $("edMsg").className = "";
    $("edPw").value = "";
    try{ const r = await fetch(`data/editor.json?t=${Date.now()}`, {cache:"no-store"}); editorBlob = r.ok ? await r.json() : null; }
    catch(e){ editorBlob = null; }
    $("edSetup").style.display = editorBlob ? "none" : "block";
    $("edReplace").style.display = editorBlob ? "block" : "none";
    $("edTok").value = "";
    $("edModal").style.display = "flex";
    setTimeout(() => (editorBlob ? $("edPw") : $("edTok")).focus(), 50);
  };
  const showSetup = () => { $("edSetup").style.display = "block"; $("edReplace").style.display = "none"; $("edTok").focus(); };
  $("edReplaceLink").onclick = e => { e.preventDefault(); showSetup(); };
  $("edCancel").onclick = () => { $("edModal").style.display = "none"; };
  $("edPw").addEventListener("keydown", e => { if (e.key === "Enter") $("edGo").click(); });
  $("edGo").onclick = async () => {
    const pw = $("edPw").value, msg = $("edMsg");
    const say = (t, cls="") => { msg.textContent = t; msg.className = cls; };
    if (!pw) return say("Saisissez le mot de passe.", "err");
    $("edGo").disabled = true;
    try{
      let token;
      const newTok = $("edTok").value.trim();
      if (editorBlob && !newTok){
        say("Vérification du mot de passe…");
        token = await decryptToken(editorBlob, pw);
      } else {
        token = newTok;
        if (!token) throw new Error("Collez le jeton GitHub.");
        if (editorBlob) await decryptToken(editorBlob, pw);  // remplacement : il faut connaître le mot de passe actuel
        say("Configuration de l'enregistrement…");
        const blob = await encryptToken(token, pw);
        const cur = await gh("data/editor.json", token);
        await putFile("data/editor.json", JSON.stringify(blob, null, 1) + "\n", cur && cur.sha, token,
          "Éditeur web : configuration de l'enregistrement");
        editorBlob = blob;
      }
      say("Enregistrement…");
      const today = new Date().toISOString().slice(0,10);
      const nPos = Object.keys(pending).length, nEx = Object.keys(pendingExpl).length;
      if (nPos){
        const cur = await gh("data/positions.json", token);
        const remote = cur ? JSON.parse(b64utf8(cur.content) || "{}") : {};
        Object.entries(pending).forEach(([k, v]) => {
          if (v === null) delete remote[k];
          else remote[k] = {lat:v.lat, lon:v.lon, date:today, nom:v.nom, insee:v.insee};
        });
        const sorted = Object.fromEntries(Object.keys(remote).sort().map(k => [k, remote[k]]));
        await putFile("data/positions.json", JSON.stringify(sorted, null, 1) + "\n", cur && cur.sha, token,
          `Positions : ${nPos} correction(s) via l'éditeur web`);
        POS = sorted; pending = {};
      }
      if (nEx){
        const cur = await gh("data/exploitants.csv", token);
        const lines = (cur ? b64utf8(cur.content) : "cle;exploitant\n").replace(/\r/g, "").split("\n");
        while (lines.length && lines[lines.length - 1] === "") lines.pop();
        const todo = Object.fromEntries(Object.entries(pendingExpl).map(([k, v]) => [upk(k), [k, v]]));
        const kept = lines.filter(l => {
          const i = l.indexOf(";");
          return i < 1 || l.startsWith("#") || !(upk(l.slice(0, i)) in todo);
        });
        Object.values(todo).forEach(([k, v]) => { if (v !== null) kept.push(`${k.replace(/;/g, ",")};${v.replace(/;/g, ",")}`); });
        await putFile("data/exploitants.csv", kept.join("\n") + "\n", cur && cur.sha, token,
          `Exploitants : ${nEx} saisie(s) via l'éditeur web`);
        EXPL = parseCsv(kept.join("\n")); pendingExpl = {};
      }
      markers.forEach(styleMoved); refreshBar();
      say("Enregistré. Visible par tous après le redéploiement GitHub Pages (≈ 1 min).", "ok");
      setTimeout(() => { $("edModal").style.display = "none"; }, 2200);
    }catch(e){
      say(e.message || String(e), "err");
      if (/Jeton GitHub refusé/.test(e.message || "")) showSetup();
    }finally{
      $("edGo").disabled = false; $("edPw").value = "";
    }
  };

  return {load, applyTo, attach, keyOf};
})();
