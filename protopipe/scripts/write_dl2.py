#!/usr/bin/env python

from sys import exit
from glob import glob
import signal
from astropy.coordinates.angle_utilities import angular_separation
import yaml
import tables as tb

# ctapipe
from ctapipe.io import EventSourceFactory
from ctapipe.utils.CutFlow import CutFlow
from ctapipe.reco.energy_regressor import *

# Utilities
from protopipe.pipeline import EventPreparer
from protopipe.pipeline.utils import (make_argparser,
                                      prod3b_tel_ids,
                                      str2bool,
                                      load_config,
                                      SignalHandler)


def main():

    # Argument parser
    parser = make_argparser()
    parser.add_argument('--regressor_dir', default='./', help='regressors directory')
    parser.add_argument('--classifier_dir', default='./', help='regressors directory')
    parser.add_argument('--force_tailcut_for_extended_cleaning', type=str2bool,
                        default=False,
                        help="For tailcut cleaning for energy/score estimation")
    args = parser.parse_args()

    # Read configuration file
    cfg = load_config(args.config_file)

    # Read site layout
    site = cfg['General']['site']
    array = cfg['General']['array']

    # Add force_tailcut_for_extended_cleaning in configuration
    cfg['General']['force_tailcut_for_extended_cleaning'] = \
        args.force_tailcut_for_extended_cleaning
    cfg['General']['force_mode'] = 'tail'

    force_mode = args.mode
    if cfg['General']['force_tailcut_for_extended_cleaning'] is True:
        force_mode = 'tail'

    if args.infile_list:
        filenamelist = []
        for f in args.infile_list:
            filenamelist += glob("{}/{}".format(args.indir, f))
        filenamelist.sort()

    if not filenamelist:
        print("no files found; check indir: {}".format(args.indir))
        exit(-1)

    # keeping track of events and where they were rejected
    evt_cutflow = CutFlow("EventCutFlow")
    img_cutflow = CutFlow("ImageCutFlow")

    # Event preparer
    preper = EventPreparer(
        config=cfg,
        mode=args.mode,
        event_cutflow=evt_cutflow,
        image_cutflow=img_cutflow)

    classifier_files = args.classifier_dir + "/classifier_{mode}_{cam_id}_{classifier}.pkl.gz"
    clf_file = classifier_files.format(
        **{"mode": force_mode,
           "wave_args": "mixed",
           "classifier": "AdaBoostClassifier",
           "cam_id": "{cam_id}"}
    )
    classifier = EnergyRegressor.load(clf_file, cam_id_list=args.cam_ids)

    regressor_files = args.regressor_dir + "/regressor_{mode}_{cam_id}_{regressor}.pkl.gz"
    reg_file = regressor_files.format(
        **{"mode": force_mode,
           "wave_args": "mixed",
           "regressor": "AdaBoostRegressor",
           "cam_id": "{cam_id}"})

    regressor = EnergyRegressor.load(reg_file, cam_id_list=args.cam_ids)

    # catch ctr-c signal to exit current loop and still display results
    signal_handler = SignalHandler()
    signal.signal(signal.SIGINT, signal_handler)

    # this class defines the reconstruction parameters to keep track of
    class RecoEvent(tb.IsDescription):
        obs_id = tb.Int16Col(dflt=-1, pos=0)
        event_id = tb.Int32Col(dflt=-1, pos=1)
        NTels_trig = tb.Int16Col(dflt=0, pos=2)
        NTels_reco = tb.Int16Col(dflt=0, pos=3)
        NTels_reco_lst = tb.Int16Col(dflt=0, pos=4)
        NTels_reco_mst = tb.Int16Col(dflt=0, pos=5)
        NTels_reco_sst = tb.Int16Col(dflt=0, pos=6)
        mc_energy = tb.Float32Col(dflt=np.nan, pos=7)
        reco_energy = tb.Float32Col(dflt=np.nan, pos=8)
        reco_alt = tb.Float32Col(dflt=np.nan, pos=9)
        reco_az = tb.Float32Col(dflt=np.nan, pos=10)
        offset = tb.Float32Col(dflt=np.nan, pos=11)
        xi = tb.Float32Col(dflt=np.nan, pos=12)
        ErrEstPos = tb.Float32Col(dflt=np.nan, pos=13)
        ErrEstDir = tb.Float32Col(dflt=np.nan, pos=14)
        gammaness = tb.Float32Col(dflt=np.nan, pos=15)
        success = tb.BoolCol(dflt=False, pos=16)
        score = tb.Float32Col(dflt=np.nan, pos=17)
        h_max = tb.Float32Col(dflt=np.nan, pos=18)
        reco_core_x = tb.Float32Col(dflt=np.nan, pos=19)
        reco_core_y = tb.Float32Col(dflt=np.nan, pos=20)
        mc_core_x = tb.Float32Col(dflt=np.nan, pos=21)
        mc_core_y = tb.Float32Col(dflt=np.nan, pos=22)

    channel = "gamma" if "gamma" in " ".join(filenamelist) else "proton"
    reco_outfile = tb.open_file(
        mode="w",
        # if no outfile name is given (i.e. don't to write the event list to disk),
        # need specify two "driver" arguments
        **({"filename": args.outfile} if args.outfile else
           {"filename": "no_outfile.h5",
            "driver": "H5FD_CORE", "driver_core_backing_store": False}))

    reco_table = reco_outfile.create_table("/", "reco_events", RecoEvent)
    reco_event = reco_table.row

    # Telescopes in analysis
    allowed_tels = set(prod3b_tel_ids(array, site=site))
    for i, filename in enumerate(filenamelist):
        # print(f"file: {i} filename = {filename}")

        source = EventSourceFactory.produce(input_url=filename,
                                            allowed_tels=allowed_tels,
                                            max_events=args.max_events)

        # loop that cleans and parametrises the images and performs the reconstruction
        for (event, n_pixel_dict, hillas_dict, n_tels,
             tot_signal, max_signals, n_cluster_dict,
             reco_result, impact_dict) in preper.prepare_event(source):

            # Angular quantities
            run_array_direction = event.mcheader.run_array_direction

            xi = angular_separation(
                event.mc.az,
                event.mc.alt,
                reco_result.az,
                reco_result.alt
            )

            offset = angular_separation(
                run_array_direction[0],  # az
                run_array_direction[1],  # alt
                reco_result.az,
                reco_result.alt
            )

            # Height of shower maximum
            h_max = reco_result.h_max

            if hillas_dict is not None:

                # Estimate particle energy
                energy_tel = np.zeros(len(hillas_dict.keys()))
                weight_tel = np.zeros(len(hillas_dict.keys()))

                for idx, tel_id in enumerate(hillas_dict.keys()):
                    cam_id = event.inst.subarray.tel[tel_id].camera.cam_id
                    moments = hillas_dict[tel_id]
                    model = regressor.model_dict[cam_id]

                    features_img = np.array([
                        np.log10(moments.intensity),
                        np.log10(impact_dict[tel_id].value),
                        moments.width.value,
                        moments.length.value,
                        h_max.value
                    ])
                    energy_tel[idx] = model.predict([features_img])
                    weight_tel[idx] = moments.intensity

                reco_energy = np.sum(weight_tel * energy_tel) / sum(weight_tel)

                # Estimate particle score
                score_tel = np.zeros(len(hillas_dict.keys()))
                weight_tel = np.zeros(len(hillas_dict.keys()))

                for idx, tel_id in enumerate(hillas_dict.keys()):
                    cam_id = event.inst.subarray.tel[tel_id].camera.cam_id
                    moments = hillas_dict[tel_id]
                    model = classifier.model_dict[cam_id]
                    features_img = np.array([
                        np.log10(reco_energy),
                        moments.width.value,
                        moments.length.value,
                        moments.skewness,
                        moments.kurtosis,
                        h_max.value
                    ])
                    score_tel[idx] = model.decision_function([features_img])
                    weight_tel[idx] = moments.intensity

                score = np.sum(weight_tel * score_tel) / sum(weight_tel)

                shower = event.mc
                mc_core_x = shower.core_x
                mc_core_y = shower.core_y

                reco_core_x = reco_result.core_x
                reco_core_y = reco_result.core_y

                alt, az = reco_result.alt, reco_result.az

                reco_event["NTels_trig"] = len(event.dl0.tels_with_data)
                reco_event["NTels_reco"] = len(hillas_dict)
                reco_event["NTels_reco_lst"] = n_tels["LST"]
                reco_event["NTels_reco_mst"] = n_tels["MST"]
                reco_event["NTels_reco_sst"] = n_tels["SST"]
                reco_event["reco_energy"] = reco_energy
                reco_event["reco_alt"] = alt.to('deg').value
                reco_event["reco_az"] = az.to('deg').value
                reco_event["offset"] = offset.to('deg').value
                reco_event["xi"] = xi.to('deg').value
                reco_event["h_max"] = h_max.to('m').value
                reco_event["reco_core_x"] = reco_core_x.to('m').value
                reco_event["reco_core_y"] = reco_core_y.to('m').value
                reco_event["mc_core_x"] = mc_core_x.to('m').value
                reco_event["mc_core_y"] = mc_core_y.to('m').value
                reco_event["score"] = score
                reco_event["success"] = True
                reco_event["ErrEstPos"] = np.nan
                reco_event["ErrEstDir"] = np.nan
            else:
                reco_event["success"] = False

            # save basic event infos
            reco_event["mc_energy"] = event.mc.energy.to('TeV').value
            reco_event["event_id"] = event.r1.event_id
            reco_event["obs_id"] = event.r1.obs_id

            reco_table.flush()
            reco_event.append()

            if signal_handler.stop:
                break
        if signal_handler.stop:
            break

    # make sure everything gets written out nicely
    reco_table.flush()

    try:
        print()
        evt_cutflow()
        print()
        img_cutflow()

    except ZeroDivisionError:
        pass

    print('Job done!')


if __name__ == '__main__':
    main()
