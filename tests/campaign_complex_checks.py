"""Independent comparisons against original inputs, never generated answers."""
import json
from pathlib import Path
import numpy as np
from PIL import Image


def has_transparent_subject(image):
    """Alpha delivery requires both background and visible content, not padding."""
    return (0 < image.get('transparent_fraction', 0) < 1
            and image.get('visible_fraction', 0) > 0)


def check(case, selected, images, audios, fixture, frame, pcm):
    checks,errors={},[]
    if not selected:return checks,errors
    target=Path(selected['local_file'])
    def pixels(path,t):return np.asarray(frame(path,t),dtype=float)
    def compare_timeline(mapping,tolerance=10):
        scores=[]
        for out,source,stamp in mapping:
            actual=pixels(target,out);wanted=pixels(fixture/source,stamp)
            scores.append(float(np.abs(actual-wanted).mean()))
        checks['source_frame_mae']=scores
        if max(scores)>tolerance:errors.append('Timeline content differs from requested source segments/order/speed')
    if case=='C01':compare_timeline([(2,'clip_a.mp4',4),(6,'clip_a.mp4',12),(10,'clip_a.mp4',22)])
    if case=='C02':
        actual=pixels(target,5.5);a=pixels(fixture/'clip_a.mp4',5.5);b=pixels(fixture/'clip_b.mp4',.5)
        blend=(a+b)/2;checks['transition_blend_mae']=float(np.abs(actual-blend).mean())
        if checks['transition_blend_mae']>16:errors.append('Crossfade midpoint does not contain expected mixed frames')
    if case=='C13':
        compare_timeline([(3,'clip_a.mp4',3),(7,'clip_a.mp4',8),(15,'clip_a.mp4',15)],12)
        still=float(np.abs(pixels(target,21.2)-pixels(target,22.5)).mean());checks['freeze_frame_mae']=still
        if still>2:errors.append('Last two seconds are not a freeze frame')
        audio=pcm(target);pitches=[]
        for seconds in (4,8.25,10):
            section=audio[int(seconds*16000):int((seconds+.5)*16000)]
            spectrum=np.abs(np.fft.rfft(section*np.hanning(len(section))))
            pitches.append(float(np.fft.rfftfreq(len(section),1/16000)[int(np.argmax(spectrum))]))
        checks['normal_fast_slow_reference_pitch_hz']=pitches
        if any(abs(value-440)>8 for value in pitches):errors.append('Pitch changed in normal/fast/slow audio sections')

    if case=='C14':
        compare_timeline([(2,'clip_a.mp4',2),(6,'clip_b.mp4',3.96),(9,'clip_b.mp4',.96),(12,'clip_a.mp4',12)],12)
        reverse=[];forward=[]
        for out,back,ahead in ((6,3.96,1),(9,.96,4)):
            actual=pixels(target,out)
            reverse.append(float(np.abs(actual-pixels(fixture/'clip_b.mp4',back)).mean()))
            forward.append(float(np.abs(actual-pixels(fixture/'clip_b.mp4',ahead)).mean()))
        checks['reverse_match_mae']=reverse;checks['incorrect_forward_match_mae']=forward
        if sum(reverse)>=sum(forward)*.9:errors.append('Middle segment does not match reverse playback better than forward playback')
    if case=='C16':
        actual=pcm(target);source=pcm(fixture/'speech_noisy.wav');clean=pcm(fixture/'speech.wav')
        n=min(len(actual),len(source),len(clean));actual=actual[:n];source=source[:n];clean=clean[:n]
        if not np.all(np.isfinite(actual)) or np.max(np.abs(actual),initial=0)<1e-9:
            return checks,['Denoised audio has no finite nonzero speech signal']
        # FFT filters can introduce a short fixed delay without changing the
        # speech. Measure it explicitly; do not confuse phase alignment with
        # speech preservation or allow arbitrary timeline warping.
        from scipy.signal import correlate,correlation_lags
        correlation=correlate(actual,clean,mode='full',method='fft')
        lags=correlation_lags(n,n,mode='full');allowed=np.abs(lags)<=800  # 50ms at16kHz
        lag=int(lags[allowed][np.argmax(correlation[allowed])])
        checks['filter_delay_samples']=lag;checks['filter_delay_ms']=lag/16
        if lag>0:actual=actual[lag:];source=source[:-lag];clean=clean[:-lag]
        elif lag<0:actual=actual[:lag];source=source[-lag:];clean=clean[-lag:]
        n=len(actual)
        # Exclude fade ends and compare only genuine original silence, avoiding speech removal as a noise metric.
        block=1600;quiet=[];voice=[]
        for start in range(16000,n-16000,block):
            end=start+block
            if end>n:break
            rms=float(np.sqrt(np.mean(clean[start:end]**2)))
            (quiet if rms<.0001 else voice).extend(range(start,end))
        if quiet:
            noise=float(np.sqrt(np.mean(actual[quiet]**2))/max(1e-9,np.sqrt(np.mean(source[quiet]**2))))
            gain=float(np.dot(actual[voice],clean[voice])/max(1e-9,np.dot(source[voice],clean[voice]))) if voice else 1
            checks['quiet_region_absolute_noise_ratio']=noise
            checks['quiet_region_noise_ratio_at_equal_voice_gain']=noise/max(abs(gain),1e-9)
            if checks['quiet_region_noise_ratio_at_equal_voice_gain']>=.85:errors.append('Noise relative to preserved speech was not substantially reduced')
        if voice:
            corr=float(np.corrcoef(clean[voice],actual[voice])[0,1]);checks['voice_correlation']=corr if np.isfinite(corr) else None
            if not np.isfinite(corr) or corr<.65:errors.append('Denoising failed to preserve speech waveform')
    if case=='C18' and audios:
        sound=pcm(Path(audios[0]['local_file']));source=pcm(fixture/'clip_a.mp4')[:12*16000];n=min(len(sound),len(source))
        ratio=float(np.sqrt(np.mean(sound[:n]**2))/np.sqrt(np.mean(source[:n]**2)));checks['audio_rms_ratio']=ratio
        if not .44<ratio<.56:errors.append('Separated audio gain differs from -6 dB')
    if case=='C19':
        import cv2
        first=np.asarray(frame(target,.6));last=np.asarray(frame(target,7.4))
        mask=np.full(first.shape[:2],255,np.uint8);mask[:110,530:]=0
        sift=cv2.SIFT_create(nfeatures=1500)
        a,da=sift.detectAndCompute(cv2.cvtColor(first,cv2.COLOR_RGB2GRAY),mask)
        b,db=sift.detectAndCompute(cv2.cvtColor(last,cv2.COLOR_RGB2GRAY),mask)
        matches=[] if da is None or db is None else [x for x,y in cv2.BFMatcher().knnMatch(da,db,k=2) if x.distance<.7*y.distance]
        matrix=inliers=None
        if len(matches)>=8:
            matrix,inliers=cv2.estimateAffinePartial2D(np.float32([a[x.queryIdx].pt for x in matches]),np.float32([b[x.trainIdx].pt for x in matches]),method=cv2.RANSAC,ransacReprojThreshold=1.5)
        if matrix is None or int(inliers.sum())<12:errors.append('Cannot independently establish poster animation motion')
        else:
            scale=float(np.hypot(matrix[0,0],matrix[0,1]));checks['poster_zoom_scale']=scale;checks['poster_motion_inliers']=int(inliers.sum())
            checks['poster_translation']=matrix[:,2].tolist()
            if not 1.005<scale<1.1:errors.append('Poster animation lacks the requested gradual small zoom')
    if case=='C21':
        picture=pixels(target,4);left=picture[80:300,:640];right=picture[80:300,640:]
        checks['left_channel_spread']=float((left.max(2)-left.min(2)).mean());checks['right_channel_spread']=float((right.max(2)-right.min(2)).mean())
        if checks['left_channel_spread']>4 or checks['right_channel_spread']<10:errors.append('Expected grayscale left branch and colored right branch')
    if case=='C23':
        actual=pixels(target,5);source=pixels(fixture/'clip_a.mp4',5)
        roi=(slice(50,250),slice(190,400));gradient=lambda p:float(np.abs(np.diff(p,axis=0)).mean()+np.abs(np.diff(p,axis=1)).mean())
        checks['blur_region_gradient_ratio']=gradient(actual[roi])/max(1e-8,gradient(source[roi]))
        outside=np.ones(actual.shape[:2],dtype=bool);outside[35:265,175:415]=False;outside[:120,510:]=False
        checks['outside_blur_mae']=float(np.abs(actual[outside]-source[outside]).mean())
        if checks['blur_region_gradient_ratio']>.6:errors.append('Specified region is not sufficiently blurred')
        if checks['outside_blur_mae']>8:errors.append('Pixels outside blur/overlay area unexpectedly changed')
    if case=='C26':
        masks=[a for a in images if 'mask' in a['name'].lower() and Path(a['local_file'])!=target]
        if masks:
            im=Image.open(masks[0]['local_file']).convert('L');mask=np.asarray(im)>0;source=np.asarray(Image.open(fixture/'poster.png').convert('RGB'),dtype=float);actual=np.asarray(Image.open(target).convert('RGB'),dtype=float)
            if mask.shape==source.shape[:2]:
                checks['outside_edit_mask_mae']=float(np.abs(actual[~mask]-source[~mask]).mean())
                if checks['outside_edit_mask_mae']>.1:errors.append('Generative erase changed pixels outside its explicit mask')
        else:errors.append('No independently inspectable erase mask delivered')
    return checks,errors
