nohup python -u ct_cleanser.py -dataset cifar10 -poison_type adaptive_blend -poison_rate 0.003 -cover_rate 0.003 -devices 0 -debug_info -alpha 0.15 -test_alpha 0.2 > logs/cifar/adaptive_blend.out 2>&1 &
nohup python -u ct_cleanser.py -dataset cifar10 -poison_type adaptive_patch -poison_rate 0.003 -cover_rate 0.006 -devices 1 -debug_info > logs/cifar/adaptive_patch.out 2>&1 &
nohup python -u ct_cleanser.py -dataset gtsrb -poison_type adaptive_blend -poison_rate 0.003 -cover_rate 0.003 -devices 2 -debug_info -alpha 0.15 -test_alpha 0.2 > logs/gtsrb/adaptive_blend.out 2>&1 &
nohup python -u ct_cleanser.py -dataset gtsrb -poison_type adaptive_patch -poison_rate 0.005 -cover_rate 0.01 -devices 3 -debug_info > logs/gtsrb/adaptive_patch.out 2>&1 &