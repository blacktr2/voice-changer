import eventlet
import socketio
import sys, os, struct, argparse
from distutils.util import strtobool
from datetime import datetime
from OpenSSL import SSL, crypto

import torch
import numpy as np
from scipy.io.wavfile import write

sys.path.append("mod")
sys.path.append("mod/text")
import utils
from data_utils import TextAudioSpeakerLoader, TextAudioSpeakerCollate
from models import SynthesizerTrn
from text.symbols import symbols

class MyCustomNamespace(socketio.Namespace): 
    def __init__(self, namespace, config, model):
        super().__init__(namespace)
        self.hps =utils.get_hparams_from_file(config)
        self.net_g = SynthesizerTrn(
                len(symbols),
                self.hps.data.filter_length // 2 + 1,
                self.hps.train.segment_size // self.hps.data.hop_length,
                n_speakers=self.hps.data.n_speakers,
                **self.hps.model)
        self.net_g.eval()
        self.gpu_num = torch.cuda.device_count()
        print("GPU_NUM:",self.gpu_num)
        utils.load_checkpoint( model, self.net_g, None)

    def on_connect(self, sid, environ):
        print('[{}] connet sid : {}'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S') , sid))
        # print('[{}] connet env : {}'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S') , environ))

    def on_request_message(self, sid, msg): 
        # print("MESSGaa", msg)
        gpu = int(msg[0])
        srcId = int(msg[1])
        dstId = int(msg[2])
        timestamp = int(msg[3])
        data = msg[4]
        # print(srcId, dstId, timestamp)
        unpackedData = np.array(struct.unpack('<%sh'%(len(data) // struct.calcsize('<h') ), data))
        write("logs/received_data.wav", 24000, unpackedData.astype(np.int16))

        # self.emit('response', msg)

        if gpu<0 or self.gpu_num==0 :
            with torch.no_grad():
                dataset = TextAudioSpeakerLoader("dummy.txt", self.hps.data, no_use_textfile=True)
                data = dataset.get_audio_text_speaker_pair([ unpackedData, srcId, "a"])
                data = TextAudioSpeakerCollate()([data])
                x, x_lengths, spec, spec_lengths, y, y_lengths, sid_src = [x.cpu() for x in data]
                sid_tgt1 = torch.LongTensor([dstId]).cpu()
                audio1 = (self.net_g.cpu().voice_conversion(spec, spec_lengths, sid_src=sid_src, sid_tgt=sid_tgt1)[0][0,0].data * self.hps.data.max_wav_value).cpu().float().numpy()
        else:
            with torch.no_grad():
                dataset = TextAudioSpeakerLoader("dummy.txt", self.hps.data, no_use_textfile=True)
                data = dataset.get_audio_text_speaker_pair([ unpackedData, srcId, "a"])
                data = TextAudioSpeakerCollate()([data])
                x, x_lengths, spec, spec_lengths, y, y_lengths, sid_src = [x.cuda(gpu) for x in data]
                sid_tgt1 = torch.LongTensor([dstId]).cuda(gpu)
                audio1 = (self.net_g.cuda(gpu).voice_conversion(spec, spec_lengths, sid_src=sid_src, sid_tgt=sid_tgt1)[0][0,0].data * self.hps.data.max_wav_value).cpu().float().numpy()

        audio1 = audio1.astype(np.int16)
        bin = struct.pack('<%sh'%len(audio1), *audio1)

        # print("return timestamp", timestamp)        
        self.emit('response',[timestamp, bin])




    def on_disconnect(self, sid):
        # print('[{}] disconnect'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        pass;

def setupArgParser():
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", type=int, required=True, help="port")
    parser.add_argument("-c", type=str, required=True, help="path for the config.json")
    parser.add_argument("-m", type=str, required=True, help="path for the model file")
    parser.add_argument("--https", type=strtobool, default=False, help="use https")
    parser.add_argument("--httpsKey", type=str, default="ssl.key", help="path for the key of https")
    parser.add_argument("--httpsCert", type=str, default="ssl.cert", help="path for the cert of https")
    parser.add_argument("--httpsSelfSigned", type=strtobool, default=True, help="generate self-signed certificate")
    return parser

def create_self_signed_cert(certfile, keyfile, certargs, cert_dir="."):
    C_F = os.path.join(cert_dir, certfile)
    K_F = os.path.join(cert_dir, keyfile)
    if not os.path.exists(C_F) or not os.path.exists(K_F):
        k = crypto.PKey()
        k.generate_key(crypto.TYPE_RSA, 2048)
        cert = crypto.X509()
        cert.get_subject().C = certargs["Country"]
        cert.get_subject().ST = certargs["State"]
        cert.get_subject().L = certargs["City"]
        cert.get_subject().O = certargs["Organization"]
        cert.get_subject().OU = certargs["Org. Unit"]
        cert.get_subject().CN = 'Example'
        cert.set_serial_number(1000)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(315360000)
        cert.set_issuer(cert.get_subject())
        cert.set_pubkey(k)
        cert.sign(k, 'sha1')
        open(C_F, "wb").write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
        open(K_F, "wb").write(crypto.dump_privatekey(crypto.FILETYPE_PEM, k))


if __name__ == '__main__':
    parser = setupArgParser()
    args = parser.parse_args()
    PORT = args.p
    CONFIG = args.c
    MODEL  = args.m
    print(f"start... PORT:{PORT}, CONFIG:{CONFIG}, MODEL:{MODEL}")

    if args.https and args.httpsSelfSigned == 1:
        # HTTPS(おれおれ証明書生成) 
        os.makedirs("./key", exist_ok=True)
        key_base_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        keyname = f"{key_base_name}.key"
        certname = f"{key_base_name}.cert"
        create_self_signed_cert(certname, keyname, certargs=
                            {"Country": "JP",
                             "State": "Tokyo",
                             "City": "Chuo-ku",
                             "Organization": "F",
                             "Org. Unit": "F"}, cert_dir="./key")
        key_path = os.path.join("./key", keyname)
        cert_path = os.path.join("./key", certname)
        print(f"protocol: HTTPS(self-signed), key:{key_path}, cert:{cert_path}")
    elif args.https and args.httpsSelfSigned == 0:
        # HTTPS 
        key_path = args.httpsKey
        cert_path = args.httpsCert
        print(f"protocol: HTTPS, key:{key_path}, cert:{cert_path}")
    else:
        # HTTP
        print("protocol: HTTP")


    # SocketIOセットアップ
    sio = socketio.Server(cors_allowed_origins='*') 
    sio.register_namespace(MyCustomNamespace('/test', CONFIG, MODEL)) 
    app = socketio.WSGIApp(sio,static_files={
        '': '../frontend/dist',
        '/': '../frontend/dist/index.html',
    }) 

    if args.https:
        # HTTPS サーバ起動
        sslWrapper = eventlet.wrap_ssl(
                eventlet.listen(('0.0.0.0',int(PORT))),
                certfile=cert_path, 
                keyfile=key_path, 
                # server_side=True
            )
        eventlet.wsgi.server(sslWrapper, app)     
    else:
        # HTTP サーバ起動
        eventlet.wsgi.server(eventlet.listen(('0.0.0.0',int(PORT))), app) 
