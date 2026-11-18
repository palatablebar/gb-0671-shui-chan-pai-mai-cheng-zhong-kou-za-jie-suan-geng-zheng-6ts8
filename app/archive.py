import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    admission=result.get('admission') or {}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine'],
            'admission':{'rows':admission.get('rows',[]),'lots':admission.get('lots',[]),'sessions':admission.get('sessions',[]),
                         'audit':admission.get('audit',[]),'weighing_evidence':admission.get('evidence',[])}},
            ensure_ascii=False,indent=2))
    return buffer.getvalue()
