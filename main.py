from scripts.train_test import train, test


if __name__ == '__main__':
    config = ""

    train(conf_file=config)
    test(conf_file=config)