(function(){
  "use strict";
  var content=document.getElementById("content");
  if(!content)return;
  var viewer=document.createElement("section");
  viewer.id="browser-view";
  viewer.setAttribute("aria-label","Virtuális böngésző");
  viewer.innerHTML='<div id="browser-toolbar"><button id="browser-toggle" type="button" aria-expanded="false"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>Böngésző</button><span id="browser-state"></span><select id="browser-tabs" aria-label="Böngésző kiválasztása" hidden></select></div><div id="browser-stage" hidden><canvas id="browser-canvas" width="1280" height="1029" tabindex="0" aria-label="Élő felhőböngésző"></canvas><button id="browser-control" type="button">Take control</button></div><div id="browser-empty" hidden>Nincs aktív felhőböngésző.</div>';
  content.insertBefore(viewer,content.firstChild);
  var status=document.createElement("div");
  status.id="work-progress";
  status.hidden=true;
  status.setAttribute("role","status");
  status.setAttribute("aria-live","polite");
  status.innerHTML='<span id="work-progress-text"></span><span id="work-progress-time"></span>';
  var composer=document.getElementById("ib-wrap");
  content.insertBefore(status,composer);
  var stage=document.getElementById("browser-stage"), canvas=document.getElementById("browser-canvas"), control=document.getElementById("browser-control"), state=document.getElementById("browser-state"), toggle=document.getElementById("browser-toggle"), tabs=document.getElementById("browser-tabs"), empty=document.getElementById("browser-empty");
  var drawing=canvas.getContext("2d"), session=null, conversation=null, opened=false, mode="agent", polling=false, generation=0, inputQueue=Promise.resolve(), progressJob=null;
  var remembered=null;
  try{remembered=sessionStorage.getItem("chat_live_conversation");}catch(e){}
  function work(event){
    if(event.type==="progress"){
      progressJob=event.task_id;
      status.hidden=false;
      document.getElementById("work-progress-text").textContent=event.message;
      var seconds=Math.floor((event.elapsed_ms||0)/1000);
      document.getElementById("work-progress-time").textContent=Math.floor(seconds/60)+":"+String(seconds%60).padStart(2,"0");
    }
    if((event.type==="done"||event.type==="terminal"||event.type==="error")&&(!progressJob||progressJob===event.task_id))status.hidden=true;
    if(event.type==="agent_event"&&event.event&&event.event.type==="tool_result"&&event.event.tool==="cloud_browser_start"){
      try{var output=JSON.parse(event.event.output);var result=output.result||output;if(result.viewer_id){selectSession(result.viewer_id);opened=true;show();}}catch(e){}
    }
  }
  window.chatWorkEvent=work;
  function show(){
    toggle.setAttribute("aria-expanded",String(opened));
    stage.hidden=!opened||!session;
    empty.hidden=!opened||!!session;
  }
  function selectSession(id){
    if(session===id)return;
    session=id;generation++;mode="agent";
    delete canvas.dataset.frame;
    control.disabled=true;
    mobileInput.hidden=true;
    if(drawing)drawing.clearRect(0,0,canvas.width,canvas.height);
    state.textContent=id?"Kapcsolódás…":"";
    show();
  }
  toggle.addEventListener("click",function(){opened=!opened;show();});
  tabs.addEventListener("change",function(){selectSession(tabs.value);opened=true;show();});
  async function request(url,options){
    var response=await fetch(url,Object.assign({credentials:"same-origin",signal:AbortSignal.timeout(18000)},options));
    var result=await response.json();
    if(!response.ok)throw new Error(result.error||"A böngésző nem érhető el.");
    return result;
  }
  async function discover(){
    var app=window.chatApp;
    if(app&&!app.getConversationId()&&remembered){
      var entry=app.getHistory().find(function(item){return item.conversation_id===remembered;});
      if(entry){app.loadConversation(entry.id);remembered=null;}
    }
    var id=app&&app.getConversationId();
    if(id!==conversation){conversation=id;selectSession(null);status.hidden=true;try{if(id)sessionStorage.setItem("chat_live_conversation",id);else sessionStorage.removeItem("chat_live_conversation");}catch(e){}}
    if(!id)return;
    try{
      var result=await request("/api/browser?conversation="+encodeURIComponent(id));
      if(conversation!==id)return;
      var sessions=result.sessions||[];
      tabs.replaceChildren();
      sessions.forEach(function(item,index){var option=document.createElement("option");option.value=item.id;option.textContent="Böngésző "+(index+1)+(item.status==="active"?"":" · leállt");tabs.appendChild(option);});
      tabs.hidden=sessions.length<2;
      var active=sessions.find(function(item){return item.status==="active";});
      var current=sessions.find(function(item){return item.id===session;});
      if(!current||(current.status!=="active"&&active)){
        var selected=sessions.find(function(item){return item.status==="active";})||sessions[0];
        selectSession(selected?selected.id:null);
        if(selected&&selected.status==="active"){opened=true;show();}
      }
      if(session)tabs.value=session;
    }catch(error){state.textContent=error.message;}
  }
  async function frame(){
    if(polling||!session||!opened||document.hidden)return;
    polling=true;
    var id=session,version=generation;
    try{
      var result=await request("/api/browser?id="+encodeURIComponent(id)+"&frame=1");
      if(version!==generation)return;
      mode=result.control_mode;
      stage.classList.toggle("human",mode==="human");
      control.disabled=result.status!=="active"||mode==="requested";
      control.textContent=mode==="human"?"Visszaadom az AI-nak":mode==="requested"?"Átadás folyamatban…":"Take control";
      state.textContent=result.status!=="active"?"Böngésző leállítva":mode==="human"?"Te irányítasz":mode==="requested"?"Az aktuális művelet befejezésére vár":"AI dolgozik";
      if(result.data&&drawing){
        var image=new Image();
        image.onload=function(){if(version===generation){drawing.drawImage(image,0,0,1280,1029);canvas.dataset.frame="ready";}};
        image.src="data:image/jpeg;base64,"+result.data;
      }
    }catch(error){if(version===generation)state.textContent=error.message;}
    finally{polling=false;if(version!==generation)frame();}
  }
  control.addEventListener("click",async function(){
    if(!session)return;
    control.disabled=true;
    var id=session,version=generation,returning=mode==="human";
    try{
      mode="requested";
      await inputQueue;
      await request("/api/browser",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id,mode:returning?"agent":"human"})});
      if(version!==generation)return;
      await frame();
      if(mode==="human")canvas.focus();
    }catch(error){state.textContent=error.message;control.disabled=false;}
  });
  function input(data){
    if(mode!=="human"||!session)return;
    var id=session,version=generation;
    inputQueue=inputQueue.then(function(){if(mode!=="human"||session!==id||version!==generation)return;return request("/api/browser",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id,input:data})});}).catch(function(error){if(version===generation)state.textContent=error.message;});
    return inputQueue;
  }
  function point(event){var rect=canvas.getBoundingClientRect();return {x:Math.max(0,Math.min(1280,(event.clientX-rect.left)*1280/rect.width)),y:Math.max(0,Math.min(1029,(event.clientY-rect.top)*1029/rect.height))};}
  function mouse(event,type){if(mode!=="human")return;event.preventDefault();var p=point(event);input(Object.assign({kind:"mouse",type:type,button:["left","middle","right"][event.button]||"none",buttons:event.buttons,clickCount:type==="mouseMoved"?0:1},p));}
  canvas.addEventListener("pointerdown",function(event){if(mode!=="human")return;canvas.focus();canvas.setPointerCapture(event.pointerId);mouse(event,"mousePressed");});
  canvas.addEventListener("pointerup",function(event){mouse(event,"mouseReleased");});
  canvas.addEventListener("pointercancel",function(event){mouse(event,"mouseReleased");});
  var lastMove=0;
  canvas.addEventListener("pointermove",function(event){if(Date.now()-lastMove<120)return;lastMove=Date.now();mouse(event,"mouseMoved");});
  canvas.addEventListener("contextmenu",function(event){if(mode==="human")event.preventDefault();});
  canvas.addEventListener("wheel",function(event){if(mode!=="human")return;event.preventDefault();input(Object.assign({kind:"wheel",type:"mouseWheel",deltaX:event.deltaX,deltaY:event.deltaY},point(event)));},{passive:false});
  canvas.addEventListener("keydown",function(event){
    if(mode!=="human")return;
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="v")return;
    event.preventDefault();
    if(event.key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey){input({kind:"text",text:event.key});return;}
    input({kind:"key",type:"keyDown",key:event.key,code:event.code,keyCode:event.keyCode,modifiers:(event.altKey?1:0)|(event.ctrlKey?2:0)|(event.metaKey?4:0)|(event.shiftKey?8:0)});
  });
  canvas.addEventListener("keyup",function(event){if(mode!=="human")return;event.preventDefault();if(event.key.length!==1||event.ctrlKey||event.metaKey||event.altKey)input({kind:"key",type:"keyUp",key:event.key,code:event.code,keyCode:event.keyCode,modifiers:(event.altKey?1:0)|(event.ctrlKey?2:0)|(event.metaKey?4:0)|(event.shiftKey?8:0)});});
  canvas.addEventListener("paste",function(event){if(mode!=="human")return;event.preventDefault();var text=event.clipboardData.getData("text/plain");if(text)input({kind:"text",text:text});});
  var mobileInput=document.createElement("input");
  mobileInput.type="text";mobileInput.autocomplete="off";mobileInput.setAttribute("aria-label","Szöveg a felhőböngészőbe");mobileInput.hidden=true;
  document.getElementById("browser-toolbar").appendChild(mobileInput);
  var typeButton=document.createElement("button");typeButton.type="button";typeButton.textContent="⌨";typeButton.title="Szöveg beírása";typeButton.setAttribute("aria-label","Szöveg beírása");document.getElementById("browser-toolbar").appendChild(typeButton);
  typeButton.addEventListener("click",function(){if(mode!=="human")return;mobileInput.hidden=!mobileInput.hidden;if(!mobileInput.hidden)mobileInput.focus();});
  mobileInput.addEventListener("keydown",function(event){if(event.key==="Enter"){event.preventDefault();if(mobileInput.value)input({kind:"text",text:mobileInput.value});mobileInput.value="";mobileInput.hidden=true;canvas.focus();}});
  window.setInterval(discover,3000);
  window.setInterval(frame,900);
  document.addEventListener("visibilitychange",function(){if(!document.hidden){discover();frame();}});
  discover();
})();