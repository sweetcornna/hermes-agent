'use strict';
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { encodeFrame, FrameReader, OP } = require('/opt/QQ/resources/app/snowluma-native-replacement/pipe-server.cjs');
const cfg = JSON.parse(fs.readFileSync('/app/data/native-onebot.json','utf8'));
const names=fs.readdirSync(cfg.runtimeDir).filter(s=>/^mojo\.\d+\.control\.sock$/.test(s)).filter(s=>{try{process.kill(Number(s.split('.')[1]),0);return true;}catch{return false;}});
if(names.length!==1)throw Error('Expected one live QQ pipe');
const pid=Number(names[0].split('.')[1]);
const socket=net.createConnection(path.join(cfg.runtimeDir,names[0]));
let hello=false,sent=false,finished=false;
const deadline=setTimeout(()=>finish({ok:false,error:'deadline'}),29000);
function finish(result){if(finished)return;finished=true;clearTimeout(deadline);socket.destroy();console.log(JSON.stringify(result));process.exitCode=result.ok?0:1;}
const bytes=(tag,value)=>{const b=Buffer.from(value);return Buffer.concat([Buffer.from([(tag<<3)|2,b.length]),b]);};
const info=Buffer.concat([bytes(1,bytes(2,String(cfg.selfUin))),bytes(2,'hermes-forward-connectivity-check'),Buffer.from([24,1])]);
const body=Buffer.concat([bytes(1,info),bytes(15,Buffer.from([8,2,16,2,24,9,32,0]))]);
const reader=new FrameReader(frame=>{
 if(frame.op===OP.hello){if(frame.value0!==BigInt(pid))throw Error('Wrong QQ pipe');hello=true;}
 else if(frame.op===OP.login&&hello&&frame.flags===4&&!sent){sent=true;socket.write(encodeFrame({op:OP.sendRequest,requestId:1,flags:1,cmd:'trpc.group.long_msg_interface.MsgService.SsoRecvLongMsg',body}));}
 else if(frame.op===OP.reply&&frame.requestId===1){finish({ok:frame.status===0,replyBytes:frame.body.length,responseHex:frame.body.toString('hex').slice(0,512)});}
 else if(frame.op===OP.error){finish({ok:false,error:frame.msg,status:frame.status});}
});
socket.on('data',b=>{try{reader.push(b);}catch{finish({ok:false,error:'invalid-frame'});}});
socket.on('error',()=>finish({ok:false,error:'socket-error'}));
