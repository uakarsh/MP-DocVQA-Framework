import yaml, json
import argparse


def parse_args():
    parser = argparse.ArgumentParser(description='Baselines for DocCVQAv2')

    # parser.add_argument('-c', '--config', type=str, required=True, help='Path to yml file with experiment configuration.')
    parser.add_argument('-m', '--model', type=str, required=True, help='Path to yml file with model configuration.')
    parser.add_argument('-d', '--dataset', type=str, required=True, help='Path to yml file with dataset configuration.')
    parser.add_argument('--eval_start', type=bool, required=False, default=True, help='Whether to evaluate the model before training or not.')

    return parser.parse_args()


def save_json(path, data):
    with open(path, 'w+') as f:
        json.dump(data, f)


def check_config(config):
    model_name = config['model_name'].lower()
    page_retrieval = config.get('page_retrieval', '').lower()
    if model_name not in ['hilt5', 'hi-lt5'] and page_retrieval == 'custom':
        raise ValueError("'Custom' retrieval is not allowed for {:}".format(model_name))

    elif model_name in ['hilt5', 'hi-lt5'] and page_retrieval in ['concat', 'logits']:
        raise ValueError("Hierarchical model {:} can't run by {:} retrieval type. Only 'oracle' and 'custom' are allowed.".format(model_name, page_retrieval))

    if page_retrieval in ['concat', 'logits'] and config.get('max_pages') is not None:
        print("WARNING - Max pages ({:}) value is ignored for {:} retrieval setting.".format(config.get('max_pages'), page_retrieval))

    return True


def load_config(args):
    model_config_path = "configs/models/{:}.yml".format(args.model)
    dataset_config_path = "configs/datasets/{:}.yml".format(args.dataset)
    model_config = parse_config(yaml.safe_load(open(model_config_path, "r")), args)
    dataset_config = parse_config(yaml.safe_load(open(dataset_config_path, "r")), args)
    training_config = model_config.pop('training_parameters')

    # Append and overwrite config values from argumments.
    # config = {'dataset_params': dataset_config, 'model_params': model_config, 'training_params': training_config}
    config = {**dataset_config, **model_config, **training_config}

    config = {k: v for k, v in args._get_kwargs()} | config
    config.pop('model')
    config.pop('dataset')

    check_config(config)

    return config


def parse_config(config, args):
    # Import included configs.
    for included_config_path in config.get('includes', []):
        config = load_config(included_config_path, args) | config

    return config


def correct_alignment(context, answer, start_idx, end_idx):

    if context[start_idx: end_idx] == answer:
        return [start_idx, end_idx]

    elif context[start_idx - 1: end_idx] == answer:
        return [start_idx - 1, end_idx]

    elif context[start_idx: end_idx + 1] == answer:
        return [start_idx, end_idx + 1]

    else:
        print(context[start_idx: end_idx], answer)
        return None


def time_stamp_to_hhmmss(timestamp, string=True):
    hh = int(timestamp/3600)
    mm = int((timestamp-hh*3600)/60)
    ss = int(timestamp - hh*3600 - mm*60)

    time = "{:02d}:{:02d}:{:02d}".format(hh, mm, ss) if string else [hh, mm, ss]

    return time
